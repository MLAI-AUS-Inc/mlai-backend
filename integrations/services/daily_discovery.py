from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import logging
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from core.models import ContentFactoryJob, OrganizationContentConfig, ScheduledDiscoveryDispatch, ScheduledDiscoveryDispatchState
from integrations.models import UserIntegration
from integrations.services.article_generation import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    SCHEDULED_DAILY_TRIGGER_SOURCE,
    trigger_article_generation,
)
from integrations.services.slack import SlackService
from integrations.utils import normalize_domain


logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_TIMEZONE = "Australia/Melbourne"
SCHEDULE_WINDOW_MINUTES = 5
STALE_QUEUE_TIMEOUT = timedelta(hours=2)
OPEN_DISPATCH_STATES = {
    ScheduledDiscoveryDispatchState.QUEUED,
    ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
}


@dataclass(frozen=True)
class DailyDiscoveryTarget:
    slack_user_id: str
    domain: str
    timezone_name: str


def _validate_timezone(value: str) -> Optional[str]:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        logger.warning("Ignoring unknown timezone: %s", candidate)
        return None
    return candidate


def _config_has_research_prereqs(config: Optional[OrganizationContentConfig]) -> bool:
    if not config:
        return False
    org = getattr(config, "organization", None)
    if not org:
        return False
    return bool(config.scan_summary and (org.competitors or org.seed_keywords))


def _collect_candidate_pairs() -> List[Tuple[str, str]]:
    pairs: Dict[Tuple[str, str], None] = {}

    repo_to_domains: Dict[str, set[str]] = {}
    configs = (
        OrganizationContentConfig.objects
        .select_related("organization")
        .exclude(github_repo__isnull=True)
        .exclude(github_repo="")
    )
    for config in configs:
        repo = str(config.github_repo or "").strip()
        domain = normalize_domain(getattr(config.organization, "domain", ""))
        if repo and domain:
            repo_to_domains.setdefault(repo, set()).add(domain)

    integrations = (
        UserIntegration.objects
        .exclude(github_repo__isnull=True)
        .exclude(github_repo="")
    )
    for integration in integrations:
        repo = str(integration.github_repo or "").strip()
        slack_user_id = str(integration.slack_user_id or "").strip()
        if not repo or not slack_user_id:
            continue
        for domain in sorted(repo_to_domains.get(repo, set())):
            pairs[(slack_user_id, domain)] = None

    historical_pairs = (
        ContentFactoryJob.objects
        .exclude(slack_user_id="")
        .exclude(domain="")
        .values_list("slack_user_id", "domain")
        .distinct()
    )
    for slack_user_id, domain in historical_pairs:
        normalized_domain = normalize_domain(domain)
        normalized_slack_user_id = str(slack_user_id or "").strip()
        if normalized_slack_user_id and normalized_domain:
            pairs[(normalized_slack_user_id, normalized_domain)] = None

    return sorted(pairs.keys())


def resolve_daily_discovery_timezone(
    slack_user_id: str,
    *,
    config: Optional[OrganizationContentConfig] = None,
    profile_cache: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    normalized_slack_user_id = str(slack_user_id or "").strip()
    profile_data = None
    if normalized_slack_user_id:
        if profile_cache is not None and normalized_slack_user_id in profile_cache:
            profile_data = profile_cache[normalized_slack_user_id]
        else:
            profile_data = SlackService.get_user_profile(normalized_slack_user_id)
            if profile_cache is not None:
                profile_cache[normalized_slack_user_id] = profile_data

    slack_timezone = _validate_timezone((profile_data or {}).get("tz") if isinstance(profile_data, dict) else "")
    if slack_timezone:
        return slack_timezone

    config_timezone = _validate_timezone(getattr(config, "default_timezone", "") or "")
    if config_timezone:
        return config_timezone

    return DEFAULT_SCHEDULE_TIMEZONE


def expire_stale_queued_dispatches(*, now=None) -> int:
    now = now or timezone.now()
    cutoff = now - STALE_QUEUE_TIMEOUT
    stale = ScheduledDiscoveryDispatch.objects.filter(
        state=ScheduledDiscoveryDispatchState.QUEUED,
        updated_at__lt=cutoff,
    )
    return stale.update(
        state=ScheduledDiscoveryDispatchState.FAILED_TIMEOUT,
        last_error="Timed out waiting for content-factory discovery callback.",
    )


def _is_due_for_local_morning(now, timezone_name: str) -> bool:
    local_now = now.astimezone(ZoneInfo(timezone_name))
    return local_now.hour == 8 and 0 <= local_now.minute < SCHEDULE_WINDOW_MINUTES


def due_daily_discovery_targets(*, now=None) -> List[DailyDiscoveryTarget]:
    now = now or timezone.now()
    profile_cache: Dict[str, Optional[dict]] = {}

    pairs = _collect_candidate_pairs()
    if not pairs:
        return []

    domains = {domain for _, domain in pairs}
    configs = {
        normalize_domain(config.organization.domain): config
        for config in (
            OrganizationContentConfig.objects
            .select_related("organization")
            .filter(organization__domain__in=domains)
        )
    }

    targets: List[DailyDiscoveryTarget] = []
    for slack_user_id, domain in pairs:
        config = configs.get(domain)
        if not _config_has_research_prereqs(config):
            continue
        timezone_name = resolve_daily_discovery_timezone(
            slack_user_id,
            config=config,
            profile_cache=profile_cache,
        )
        if not _is_due_for_local_morning(now, timezone_name):
            continue
        targets.append(
            DailyDiscoveryTarget(
                slack_user_id=slack_user_id,
                domain=domain,
                timezone_name=timezone_name,
            )
        )

    return targets


def _build_client_request_id(*, slack_user_id: str, domain: str, local_date: date) -> str:
    return f"scheduled-daily:{normalize_domain(domain)}:{slack_user_id}:{local_date.isoformat()}"


def _find_open_dispatch(*, slack_user_id: str, domain: str, exclude_id: Optional[int] = None):
    queryset = ScheduledDiscoveryDispatch.objects.filter(
        slack_user_id=slack_user_id,
        domain=normalize_domain(domain),
        trigger_source="daily_scheduler",
        state__in=list(OPEN_DISPATCH_STATES),
    )
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)
    return queryset.order_by("-updated_at").first()


def enqueue_scheduled_discovery(
    *,
    slack_user_id: str,
    domain: str,
    local_date: Optional[date] = None,
    timezone_name: Optional[str] = None,
    force: bool = False,
    now=None,
) -> dict:
    now = now or timezone.now()
    normalized_domain = normalize_domain(domain)
    normalized_slack_user_id = str(slack_user_id or "").strip()
    if not normalized_domain or not normalized_slack_user_id:
        return {"status": "skipped", "reason": "missing_target_fields"}

    config = (
        OrganizationContentConfig.objects
        .select_related("organization")
        .filter(organization__domain=normalized_domain)
        .first()
    )
    if not _config_has_research_prereqs(config):
        return {"status": "skipped", "reason": "missing_research_prereqs", "domain": normalized_domain}

    resolved_timezone = _validate_timezone(timezone_name or "") or resolve_daily_discovery_timezone(
        normalized_slack_user_id,
        config=config,
    )
    local_now = now.astimezone(ZoneInfo(resolved_timezone))
    resolved_local_date = local_date or local_now.date()

    with transaction.atomic():
        open_dispatch = _find_open_dispatch(
            slack_user_id=normalized_slack_user_id,
            domain=normalized_domain,
        )
        if open_dispatch:
            return {
                "status": "skipped",
                "reason": "open_suggestion_exists",
                "open_dispatch_id": open_dispatch.id,
                "dispatch_id": open_dispatch.id,
            }

        existing_same_day = None
        dispatch, created = ScheduledDiscoveryDispatch.objects.select_for_update().get_or_create(
            slack_user_id=normalized_slack_user_id,
            domain=normalized_domain,
            local_date=resolved_local_date,
            trigger_source="daily_scheduler",
            defaults={
                "timezone": resolved_timezone,
                "state": ScheduledDiscoveryDispatchState.QUEUED,
            },
        )

        if not created:
            existing_same_day = dispatch
            if not force:
                return {
                    "status": "skipped",
                    "reason": "dispatch_already_exists",
                    "dispatch_id": dispatch.id,
                    "state": dispatch.state,
                }
            if dispatch.state in OPEN_DISPATCH_STATES:
                return {
                    "status": "skipped",
                    "reason": "dispatch_already_open",
                    "dispatch_id": dispatch.id,
                    "state": dispatch.state,
                }
            dispatch.state = ScheduledDiscoveryDispatchState.QUEUED
            dispatch.timezone = resolved_timezone
            dispatch.last_error = ""
            dispatch.content_factory_job_id = ""
            dispatch.slack_channel_id = ""
            dispatch.slack_message_ts = ""
            dispatch.slack_thread_ts = ""
            dispatch.save(
                update_fields=[
                    "state",
                    "timezone",
                    "last_error",
                    "content_factory_job_id",
                    "slack_channel_id",
                    "slack_message_ts",
                    "slack_thread_ts",
                    "updated_at",
                ]
            )

    article_request = {
        "domain": normalized_domain,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        "client_request_id": _build_client_request_id(
            slack_user_id=normalized_slack_user_id,
            domain=normalized_domain,
            local_date=resolved_local_date,
        ),
        "trigger_source": SCHEDULED_DAILY_TRIGGER_SOURCE,
        "scheduled_local_date": resolved_local_date.isoformat(),
        "scheduled_timezone": resolved_timezone,
    }

    try:
        result = trigger_article_generation(normalized_slack_user_id, article_request)
        content_factory_job_id = str(result.get("job_id") or result.get("run_id") or "").strip()
        update_fields = ["timezone", "updated_at"]
        dispatch = ScheduledDiscoveryDispatch.objects.get(pk=dispatch.id)
        dispatch.timezone = resolved_timezone
        if content_factory_job_id and dispatch.content_factory_job_id != content_factory_job_id:
            dispatch.content_factory_job_id = content_factory_job_id
            update_fields.append("content_factory_job_id")
        dispatch.save(update_fields=update_fields)
        payload = {
            "status": "queued",
            "dispatch_id": dispatch.id,
            "job_id": content_factory_job_id,
            "state": dispatch.state,
            "domain": normalized_domain,
            "slack_user_id": normalized_slack_user_id,
            "local_date": resolved_local_date.isoformat(),
        }
        if existing_same_day is not None:
            payload["replayed"] = True
        return payload
    except Exception as exc:
        ScheduledDiscoveryDispatch.objects.filter(pk=dispatch.id).update(
            state=ScheduledDiscoveryDispatchState.FAILED,
            last_error=str(exc),
        )
        return {
            "status": "failed",
            "dispatch_id": dispatch.id,
            "domain": normalized_domain,
            "slack_user_id": normalized_slack_user_id,
            "error": str(exc),
        }


def run_daily_discovery_scheduler(*, now=None) -> dict:
    now = now or timezone.now()
    timed_out = expire_stale_queued_dispatches(now=now)
    targets = due_daily_discovery_targets(now=now)
    results = [enqueue_scheduled_discovery(
        slack_user_id=target.slack_user_id,
        domain=target.domain,
        timezone_name=target.timezone_name,
        now=now,
    ) for target in targets]
    return {
        "status": "ok",
        "timed_out_dispatches": timed_out,
        "targets_considered": len(targets),
        "queued": sum(1 for result in results if result.get("status") == "queued"),
        "skipped": sum(1 for result in results if result.get("status") == "skipped"),
        "failed": sum(1 for result in results if result.get("status") == "failed"),
        "results": results,
    }


def mark_scheduled_dispatch_topic_selection_sent(
    *,
    job_id: str,
    slack_channel_id: str = "",
    slack_thread_ts: str = "",
    slack_message_ts: str = "",
) -> Optional[ScheduledDiscoveryDispatch]:
    dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
    if not dispatch:
        return None

    dispatch.state = ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT
    if slack_channel_id:
        dispatch.slack_channel_id = slack_channel_id
    if slack_thread_ts:
        dispatch.slack_thread_ts = slack_thread_ts
    if slack_message_ts:
        dispatch.slack_message_ts = slack_message_ts
    dispatch.save(
        update_fields=[
            "state",
            "slack_channel_id",
            "slack_thread_ts",
            "slack_message_ts",
            "updated_at",
        ]
    )
    return dispatch


def mark_scheduled_dispatch_failed(*, job_id: str, error_message: str, timeout: bool = False) -> Optional[ScheduledDiscoveryDispatch]:
    dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
    if not dispatch:
        return None
    dispatch.state = (
        ScheduledDiscoveryDispatchState.FAILED_TIMEOUT
        if timeout
        else ScheduledDiscoveryDispatchState.FAILED
    )
    dispatch.last_error = str(error_message or "").strip()
    dispatch.save(update_fields=["state", "last_error", "updated_at"])
    return dispatch


def mark_scheduled_dispatch_confirmed(*, job_id: str) -> Optional[ScheduledDiscoveryDispatch]:
    dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
    if not dispatch:
        return None
    dispatch.state = ScheduledDiscoveryDispatchState.CONFIRMED
    dispatch.save(update_fields=["state", "updated_at"])
    return dispatch


def mark_scheduled_dispatch_cancelled(*, job_id: str) -> Optional[ScheduledDiscoveryDispatch]:
    dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
    if not dispatch:
        return None
    dispatch.state = ScheduledDiscoveryDispatchState.CANCELLED
    dispatch.save(update_fields=["state", "updated_at"])
    return dispatch


def is_scheduled_daily_job(job: Optional[ContentFactoryJob]) -> bool:
    if not job:
        return False
    request_meta = getattr(job, "request_meta", {}) or {}
    return str(request_meta.get("trigger_source") or "").strip() == SCHEDULED_DAILY_TRIGGER_SOURCE
