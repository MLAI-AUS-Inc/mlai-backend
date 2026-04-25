from __future__ import annotations

import uuid
from datetime import timedelta, timezone as datetime_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from content_factory.article_system import resolve_article_system
from content_factory.contract import CONTENT_FACTORY_REQUEST_SOURCE
from content_factory.google_baseline import collect_verified_google_metrics, google_baseline_connection_status
from content_factory.models import OrganizationContentConfig, WebsiteBaselineSnapshot
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import (
    apply_shared_startup_details,
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
from integrations.services.github import build_github_auth_url
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStep,
    ContentFactoryRunStatus,
    ContentFactoryStepStatus,
)


VIBE_MARKETING_WORKFLOWS = {
    "repo_scan",
    "content_factory_scan",
    "auto_discovery",
    "content_factory_discovery",
    "article_generation",
    "content_factory_article",
    "daily_discovery",
    "startup_autofill",
    "website_baseline",
    "vibe_marketing_daily_replay",
}
SCAN_WORKFLOWS = {"repo_scan", "content_factory_scan"}
DISCOVERY_WORKFLOWS = {"auto_discovery", "content_factory_discovery", "daily_discovery"}
ARTICLE_WORKFLOWS = {"article_generation", "content_factory_article"}
BASELINE_WORKFLOWS = {"website_baseline"}
BASELINE_FRESH_DAYS = 30


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


def _run_belongs_to_context(run, context) -> bool:
    return normalize_company_domain(run.domain) == normalize_company_domain(context.organization.domain)


def _latest_runs_for_org(organization, limit=6):
    return list(
        ContentFactoryRun.objects.filter(domain=organization.domain, workflow__in=VIBE_MARKETING_WORKFLOWS)
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


def _extract_topic_candidates_from_result(result):
    if not isinstance(result, dict):
        return []
    raw_candidates = _first_non_empty_mapping_value(
        result,
        "topic_options",
        "topics",
        "topic_candidates",
        "candidates",
        "keywords",
        "keyword_options",
    )
    if not raw_candidates and isinstance(result.get("selection_data"), dict):
        raw_candidates = _first_non_empty_mapping_value(
            result["selection_data"],
            "topic_options",
            "topics",
            "candidates",
            "keywords",
        )
    if not isinstance(raw_candidates, list):
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
            or raw.get("query")
            or raw.get("title")
            or ""
        ).strip()
        title = str(raw.get("title") or raw.get("angle") or raw.get("headline") or keyword).strip()
        if not keyword and not title:
            continue
        candidates.append(
            {
                "id": str(raw.get("id") or raw.get("keyword_id") or index),
                "keyword": keyword or title,
                "title": title or keyword,
                "reason": str(raw.get("reason") or raw.get("selection_reason") or raw.get("rationale") or ""),
                "source": str(raw.get("source") or "discovery"),
                "intent": raw.get("intent"),
                "difficulty": raw.get("difficulty"),
                "opportunityScore": raw.get("opportunity_score") or raw.get("opportunityIndex"),
                "volume": raw.get("volume"),
            }
        )
    return candidates


def _topic_candidates_from_runs(runs):
    for run in runs:
        if run.workflow not in DISCOVERY_WORKFLOWS:
            continue
        candidates = _extract_topic_candidates_from_result(run.result or {})
        if candidates:
            return candidates
    return []


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
    }


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
    next_url = "/founder-tools/marketing/create?step=baseline"
    query = urlencode({"scope": "website_baseline", "next": next_url})
    return request.build_absolute_uri(f"/integrations/connect/google?{query}")


def _serialize_startup_profile(organization):
    try:
        profile = organization.startup_profile
    except Exception:
        return {
            "founderNames": [],
            "stage": "",
            "notes": "",
            "companyAliases": [],
            "domainAliases": [],
        }
    return {
        "founderNames": list(profile.founder_names or []),
        "stage": profile.stage,
        "notes": profile.notes,
        "companyAliases": list(profile.company_aliases or []),
        "domainAliases": list(profile.domain_aliases or []),
        "competitorDomains": list(profile.competitor_domains or []),
        "positiveKeywords": list(profile.positive_keywords or []),
    }


def _serialize_run(run):
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
    topic_candidates = _topic_candidates_from_runs(latest_runs)
    research_ready = bool(topic_candidates) or bool(
        discovery_run and discovery_run.status in {
            ContentFactoryRunStatus.AWAITING_CONFIRMATION,
            ContentFactoryRunStatus.COMPLETED,
        }
    )
    article_result = (article_run.result if article_run else {}) or {}
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
        )
    )
    publish_evidence = _publish_evidence_from_run(article_run)
    publish_ready = bool(
        article_run
        and (
            article_run.approval_state == ContentFactoryApprovalState.APPROVED
            or article_run.status == ContentFactoryRunStatus.COMPLETED
        )
        and (publish_evidence.get("previewUrl") or publish_evidence.get("prUrl"))
    )
    delivery_mode = config.article_delivery_mode or getattr(settings, "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE", "publish_code")
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
        "publish": {"passed": publish_ready, "runId": article_run.run_id if article_run else None},
        "dailyAutomation": {"passed": daily_ready},
    }


def _serialize_bootstrap(context, request=None):
    config = _get_config(context.organization)
    latest_runs = _latest_runs_for_org(context.organization)
    baseline_snapshot = _latest_baseline_snapshot(context.organization)
    checks = _profile_checks(context.organization, config, latest_runs, baseline_snapshot)
    guided_steps, current_guided_step = _guided_steps(checks)
    latest_runs_by_workflow = {}
    for run in latest_runs:
        latest_runs_by_workflow.setdefault(run.workflow, _serialize_run(run))
    latest_article_run = _latest_run_matching(latest_runs, ARTICLE_WORKFLOWS)
    google_status = google_baseline_connection_status(context.profile.user)
    google_status["connectUrl"] = _google_baseline_connect_url(request)
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
            "articleDeliveryMode": config.article_delivery_mode
            or getattr(settings, "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE", "publish_code"),
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
        "latestRuns": [_serialize_run(run) for run in latest_runs],
        "latestRunsByWorkflow": latest_runs_by_workflow,
        "topicCandidates": _topic_candidates_from_runs(latest_runs),
        "publishEvidence": _publish_evidence_from_run(latest_article_run),
        "guidedSteps": guided_steps,
        "currentGuidedStep": current_guided_step,
        "recommendedNextAction": _recommended_next_action(checks),
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
            "articleDeliveryMode": getattr(settings, "CONTENT_FACTORY_DEFAULT_ARTICLE_DELIVERY_MODE", "publish_code"),
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
            "notes": "",
            "companyAliases": [company.name] if company.name else [],
            "domainAliases": [],
        },
        "websiteBaseline": {"status": "missing", "passed": False, "skipped": False},
        "googleBaselineConnection": {"connected": False, "hasBaselineScopes": False, "status": "needs_connection", "connectUrl": ""},
        "latestRuns": [],
        "latestRunsByWorkflow": {},
        "topicCandidates": [],
        "publishEvidence": {},
        "guidedSteps": guided_steps,
        "currentGuidedStep": current_guided_step,
        "recommendedNextAction": {"key": "websiteProfile", "label": "Save website profile"},
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
    for key in ("warnings", "errors", "error", "message", "preview_url", "pr_url", "route_path"):
        if remote_data.get(key) is not None and merged.get(key) is None:
            merged[key] = remote_data.get(key)
    if not merged and remote_data:
        merged = dict(remote_data)
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
        run.save(update_fields=["workflow", "domain", "github_repo", "slack_user_id", "run_request", "updated_at"])
    return run


def _call_content_factory_run_status(run_id):
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip().rstrip("/")
    should_call_remote = bool(base_url and (getattr(settings, "CONTENT_FACTORY_API_KEY", None) or not getattr(settings, "IS_LOCAL_ENV", False)))
    if not should_call_remote:
        return {}

    try:
        response = http_client.get(
            f"{base_url}/api/runs/{run_id}",
            headers=_content_factory_headers(),
            timeout=(3, 15),
        )
    except http_client.RequestException as exc:
        return {"error": str(exc), "errors": [str(exc)], "retryable": True}

    if response.status_code == 200:
        return response.json() if response.content else {}
    if response.status_code == 404:
        return {}

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
    run.status = _normalize_remote_run_status(remote_data.get("status") or result.get("status") or run.status)
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
    return run


def _queue_content_factory_run(*, endpoint, workflow, context, config, payload):
    actor_id = founder_actor_id_for_user(context.profile.user)
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip().rstrip("/")
    remote_data = {}
    should_call_remote = bool(base_url and (getattr(settings, "CONTENT_FACTORY_API_KEY", None) or not getattr(settings, "IS_LOCAL_ENV", False)))
    if should_call_remote:
        url = f"{base_url}/api/runs/{endpoint}"
        try:
            response = http_client.post(url, json=payload, headers=_content_factory_headers(), timeout=(3, 10))
            if response.status_code in (200, 202):
                remote_data = response.json() if response.content else {}
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
                remote_data = {
                    "status": ContentFactoryRunStatus.BLOCKED,
                    "error": str(detail),
                    "errors": [str(detail)],
                    "content_factory_status_code": response.status_code,
                    "content_factory_response": response_payload,
                    "retryable": response.status_code >= 500,
                }
        except http_client.RequestException as exc:
            remote_data = {
                "status": ContentFactoryRunStatus.BLOCKED,
                "error": str(exc),
                "errors": [str(exc)],
                "retryable": True,
            }

    return _create_local_run(
        workflow=workflow,
        domain=context.organization.domain,
        github_repo=config.github_repo or payload.get("github_repo") or "",
        actor_id=actor_id,
        payload=payload,
        remote_data=remote_data,
    )


def _call_content_factory_run_action(*, run_id, action, payload):
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip().rstrip("/")
    should_call_remote = bool(base_url and (getattr(settings, "CONTENT_FACTORY_API_KEY", None) or not getattr(settings, "IS_LOCAL_ENV", False)))
    if not should_call_remote:
        return {}

    try:
        response = http_client.post(
            f"{base_url}/api/runs/{run_id}/{action}",
            json=payload or {},
            headers=_content_factory_headers(),
            timeout=(3, 15),
        )
    except http_client.RequestException as exc:
        return {"error": str(exc), "errors": [str(exc)], "retryable": True}

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
        config.connected_slack_user_id = founder_actor_id_for_user(request.user)
        config.brand_name = request.data.get("brand_name", request.data.get("brandName", config.brand_name))
        config.company_context = request.data.get("company_context", request.data.get("companyContext", config.company_context))
        config.github_repo = request.data.get("github_repo", request.data.get("githubRepo", config.github_repo))
        config.article_delivery_mode = request.data.get(
            "article_delivery_mode",
            request.data.get("articleDeliveryMode", config.article_delivery_mode),
        )
        if "daily_discovery_enabled" in request.data or "dailyDiscoveryEnabled" in request.data:
            config.daily_discovery_enabled = _bool_from_request(
                request.data.get("daily_discovery_enabled", request.data.get("dailyDiscoveryEnabled"))
            )
        if request.data.get("default_timezone") or request.data.get("defaultTimezone"):
            config.default_timezone = request.data.get("default_timezone") or request.data.get("defaultTimezone")
        if config.daily_discovery_enabled:
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
        if not organization.name:
            organization.name = company_name
            organization.save(update_fields=["name"])
        company_linkedin_url = organization.company_linkedin_url
        if "company_linkedin_url" in request.data or "companyLinkedInUrl" in request.data:
            try:
                company_linkedin_url = normalize_company_linkedin_url(
                    request.data.get("company_linkedin_url", request.data.get("companyLinkedInUrl"))
                )
            except ValueError as exc:
                return Response({"detail": str(exc), "field": "companyLinkedInUrl"}, status=status.HTTP_400_BAD_REQUEST)
            if organization.company_linkedin_url != company_linkedin_url:
                organization.company_linkedin_url = company_linkedin_url
                organization.save(update_fields=["company_linkedin_url"])

        config = _get_config(organization)
        actor_id = founder_actor_id_for_user(request.user)
        config.connected_slack_user_id = actor_id
        brand_name = str(request.data.get("brand_name") or request.data.get("brandName") or "").strip()
        if brand_name:
            config.brand_name = brand_name
        config.save(update_fields=["connected_slack_user_id", "brand_name", "updated_at"])

        context = get_founder_company_context(request.user, company_id=company.id)
        existing_fields = {
            "brandName": brand_name or config.brand_name or organization.name,
            "companyContext": _request_value(
                request.data,
                "company_context",
                "companyContext",
                default=config.company_context,
            ),
            "competitors": _camel_list(_request_value(request.data, "competitors", default=organization.competitors)),
            "seedKeywords": _camel_list(
                _request_value(request.data, "seed_keywords", "seedKeywords", default=organization.seed_keywords)
            ),
            "companyLinkedInUrl": company_linkedin_url,
        }
        payload = {
            "domain": organization.domain,
            "company_name": company.name,
            "brand_name": brand_name or config.brand_name or organization.name,
            "company_linkedin_url": company_linkedin_url,
            "location": company.location,
            "abn": company.abn,
            "existing_fields": existing_fields,
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
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


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
        config.connected_slack_user_id = founder_actor_id_for_user(request.user)
        if request.data.get("github_repo") or request.data.get("githubRepo"):
            config.github_repo = request.data.get("github_repo") or request.data.get("githubRepo")
        config.save(update_fields=["connected_slack_user_id", "github_repo", "updated_at"])
        return Response(
            {"auth_url": build_github_auth_url(config.connected_slack_user_id, domain=context.organization.domain)},
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
        baseline_snapshot = _latest_baseline_snapshot(context.organization)
        if not _baseline_requirement_satisfied(config, baseline_snapshot):
            return Response(
                {
                    "detail": "Run the website baseline or skip it before generating an article.",
                    "check": _serialize_baseline_snapshot(baseline_snapshot, config),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        topic = str(request.data.get("topic") or request.data.get("keyword") or request.data.get("target_keyword") or "").strip()
        payload = {
            "domain": context.organization.domain,
            "slack_user_id": founder_actor_id_for_user(request.user),
            "topic": topic,
            "target_keyword": str(request.data.get("target_keyword") or request.data.get("targetKeyword") or topic),
            "context": str(request.data.get("context") or ""),
            "github_repo": config.github_repo,
            "delivery_mode": request.data.get("delivery_mode") or request.data.get("deliveryMode") or config.article_delivery_mode,
            "delivery_mode_confirmed": bool(request.data.get("delivery_mode_confirmed", request.data.get("deliveryModeConfirmed", True))),
            "source_discovery_run_id": request.data.get("source_discovery_run_id") or request.data.get("sourceDiscoveryRunId"),
            "title": request.data.get("title") or request.data.get("titleAngle"),
            "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        }
        run = _queue_content_factory_run(
            endpoint="article",
            workflow="article_generation",
            context=context,
            config=config,
            payload=payload,
        )
        return Response({"run_id": run.run_id, "runId": run.run_id, "status": run.status}, status=status.HTTP_202_ACCEPTED)


class VibeMarketingRunView(APIView):
    def get(self, request, run_id):
        context, error_response = _resolve_context_or_response(request, require_domain=False)
        if error_response:
            return error_response
        run = get_object_or_404(ContentFactoryRun.objects.prefetch_related("steps"), run_id=run_id)
        if not _run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        remote_data = _call_content_factory_run_status(run.run_id)
        if remote_data:
            run = _sync_local_run_from_remote(run, remote_data)
            if run.workflow in BASELINE_WORKFLOWS and run.status == ContentFactoryRunStatus.COMPLETED:
                _persist_baseline_snapshot_from_payload(organization=context.organization, run=run)
            run = ContentFactoryRun.objects.prefetch_related("steps").get(pk=run.pk)
        return Response(_serialize_run(run), status=status.HTTP_200_OK)


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
        remote_data = _call_content_factory_run_action(run_id=run_id, action=action, payload=payload)

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
                return Response(_serialize_run(revised_run), status=status.HTTP_202_ACCEPTED)

            result = run.result or {}
            revisions = list(result.get("revisions") or [])
            revisions.append(
                {
                    "submitted_at": timezone.now().isoformat(),
                    "instructions": payload.get("revision_instructions") or payload.get("revisionInstructions") or "",
                    "edited_content": payload.get("edited_content") or payload.get("editedContent") or "",
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
            return Response(_serialize_run(run), status=status.HTTP_202_ACCEPTED)

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
        return Response(_serialize_run(run), status=status.HTTP_200_OK)


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
