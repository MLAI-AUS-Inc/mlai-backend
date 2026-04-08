from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
import logging
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import (
    ContentFactoryJob,
    OrganizationContentConfig,
    ScheduledDiscoveryDispatch,
    ScheduledDiscoveryDispatchState,
)
from integrations.models import UserIntegration
from integrations.services.article_generation import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    SCHEDULED_DAILY_TRIGGER_SOURCE,
    trigger_article_generation,
)
from integrations.utils import normalize_domain


logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_TIMEZONE = "Australia/Melbourne"
DEFAULT_SCHEDULE_START_HOUR = 8
DEFAULT_SCHEDULE_START_MINUTE = 0
DEFAULT_SCHEDULE_SLOT_MINUTES = 15
DEFAULT_MAX_SCHEDULED_TARGETS = 20
STALE_QUEUE_TIMEOUT = timedelta(hours=2)
OPEN_DISPATCH_STATES = {
    ScheduledDiscoveryDispatchState.SCHEDULED,
    ScheduledDiscoveryDispatchState.QUEUED,
    ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
}


@dataclass(frozen=True)
class DailyDiscoveryTarget:
    slack_user_id: str
    domain: str
    priority: int


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


def get_daily_discovery_schedule_timezone() -> str:
    configured = _validate_timezone(
        getattr(settings, "SCHEDULED_DISCOVERY_TIMEZONE", DEFAULT_SCHEDULE_TIMEZONE)
    )
    return configured or DEFAULT_SCHEDULE_TIMEZONE


def get_daily_discovery_schedule_channel_name() -> str:
    return str(
        getattr(settings, "SCHEDULED_DISCOVERY_CHANNEL_NAME", "vibe-marketing") or "vibe-marketing"
    ).strip().lstrip("#")


def get_daily_discovery_slot_minutes() -> int:
    try:
        value = int(getattr(settings, "SCHEDULED_DISCOVERY_SLOT_MINUTES", DEFAULT_SCHEDULE_SLOT_MINUTES))
    except (TypeError, ValueError):
        value = DEFAULT_SCHEDULE_SLOT_MINUTES
    return max(1, value)


def get_daily_discovery_max_targets() -> int:
    try:
        value = int(getattr(settings, "SCHEDULED_DISCOVERY_MAX_TARGETS", DEFAULT_MAX_SCHEDULED_TARGETS))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_SCHEDULED_TARGETS
    return max(1, value)


def _schedule_zoneinfo() -> ZoneInfo:
    return ZoneInfo(get_daily_discovery_schedule_timezone())


def _schedule_local_now(now: Optional[datetime] = None) -> datetime:
    current = now or timezone.now()
    return current.astimezone(_schedule_zoneinfo())


def _schedule_start_time() -> time:
    return time(hour=DEFAULT_SCHEDULE_START_HOUR, minute=DEFAULT_SCHEDULE_START_MINUTE)


def _schedule_slot_datetime(*, local_date: date, slot_index: int) -> datetime:
    local_base = datetime.combine(local_date, _schedule_start_time(), tzinfo=_schedule_zoneinfo())
    return (local_base + timedelta(minutes=slot_index * get_daily_discovery_slot_minutes())).astimezone(
        dt_timezone.utc
    )


def _config_has_research_prereqs(config: Optional[OrganizationContentConfig]) -> bool:
    if not config:
        return False
    org = getattr(config, "organization", None)
    if not org:
        return False
    return bool(config.scan_summary and (org.competitors or org.seed_keywords))


def _record_unique_mapping(mapping: Dict[str, Optional[str]], key: str, value: str) -> None:
    normalized_key = str(key or "").strip()
    normalized_value = str(value or "").strip()
    if not normalized_key or not normalized_value:
        return
    if normalized_key in mapping and mapping[normalized_key] != normalized_value:
        mapping[normalized_key] = None
    elif normalized_key not in mapping:
        mapping[normalized_key] = normalized_value


def _build_owner_maps() -> Tuple[Dict[str, Optional[str]], Dict[str, Optional[str]], Dict[str, str]]:
    repo_to_user: Dict[str, Optional[str]] = {}
    github_user_to_slack: Dict[str, Optional[str]] = {}
    domain_to_recent_owner: Dict[str, str] = {}

    integrations = UserIntegration.objects.all()
    for integration in integrations:
        slack_user_id = str(getattr(integration, "slack_user_id", "") or "").strip()
        github_repo = str(getattr(integration, "github_repo", "") or "").strip()
        github_user_name = str(getattr(integration, "github_user_name", "") or "").strip()
        if github_repo:
            _record_unique_mapping(repo_to_user, github_repo, slack_user_id)
        if github_user_name:
            _record_unique_mapping(github_user_to_slack, github_user_name, slack_user_id)

    for dispatch in ScheduledDiscoveryDispatch.objects.exclude(slack_user_id="").exclude(domain="").order_by("-updated_at"):
        normalized_domain = normalize_domain(dispatch.domain)
        slack_user_id = str(dispatch.slack_user_id or "").strip()
        if normalized_domain and slack_user_id and normalized_domain not in domain_to_recent_owner:
            domain_to_recent_owner[normalized_domain] = slack_user_id

    source_jobs = (
        ContentFactoryJob.objects.exclude(slack_user_id="")
        .exclude(domain="")
        .order_by("-updated_at")
    )
    for job in source_jobs:
        request_meta = dict(job.request_meta or {})
        if str(request_meta.get("trigger_source") or "").strip() != SCHEDULED_DAILY_TRIGGER_SOURCE:
            continue
        if str(request_meta.get("source_run_id") or "").strip():
            continue
        normalized_domain = normalize_domain(job.domain)
        slack_user_id = str(job.slack_user_id or "").strip()
        if normalized_domain and slack_user_id and normalized_domain not in domain_to_recent_owner:
            domain_to_recent_owner[normalized_domain] = slack_user_id

    return repo_to_user, github_user_to_slack, domain_to_recent_owner


def infer_daily_discovery_owner(
    *,
    domain: str = "",
    connected_slack_user_id: str = "",
    github_repo: str = "",
    github_user_name: str = "",
    config: Optional[OrganizationContentConfig] = None,
    owner_maps: Optional[Tuple[Dict[str, Optional[str]], Dict[str, Optional[str]], Dict[str, str]]] = None,
) -> Optional[str]:
    direct_owner = str(connected_slack_user_id or "").strip() or str(
        getattr(config, "connected_slack_user_id", "") or ""
    ).strip()
    if direct_owner:
        return direct_owner

    repo_to_user, github_user_to_slack, domain_to_recent_owner = owner_maps or _build_owner_maps()

    resolved_repo = str(github_repo or "").strip() or str(getattr(config, "github_repo", "") or "").strip()
    if resolved_repo:
        owner = repo_to_user.get(resolved_repo)
        if owner:
            return owner

    resolved_github_user_name = str(github_user_name or "").strip() or str(
        getattr(config, "github_user_name", "") or ""
    ).strip()
    if resolved_github_user_name:
        owner = github_user_to_slack.get(resolved_github_user_name)
        if owner:
            return owner

    resolved_domain = normalize_domain(domain or "") or normalize_domain(
        getattr(getattr(config, "organization", None), "domain", "")
    )
    if resolved_domain:
        return domain_to_recent_owner.get(resolved_domain)

    return None


def resolve_daily_discovery_timezone(
    slack_user_id: str,
    *,
    config: Optional[OrganizationContentConfig] = None,
    profile_cache: Optional[Dict[str, Optional[dict]]] = None,
) -> str:
    """
    Retained for compatibility with existing callers. The shared queue now always
    runs on the configured schedule timezone.
    """
    del slack_user_id, config, profile_cache
    return get_daily_discovery_schedule_timezone()


def count_enabled_daily_discovery_configs(*, exclude_config_id: Optional[int] = None) -> int:
    queryset = OrganizationContentConfig.objects.filter(daily_discovery_enabled=True)
    if exclude_config_id is not None:
        queryset = queryset.exclude(pk=exclude_config_id)
    return queryset.count()


def _upsert_inferred_owner(config: OrganizationContentConfig, owner: Optional[str]) -> Optional[str]:
    normalized_owner = str(owner or "").strip()
    if normalized_owner and normalized_owner != str(config.connected_slack_user_id or "").strip():
        config.connected_slack_user_id = normalized_owner
        config.save(update_fields=["connected_slack_user_id", "updated_at"])
    return normalized_owner or None


def _eligible_daily_discovery_targets() -> tuple[List[DailyDiscoveryTarget], Dict[str, int]]:
    owner_maps = _build_owner_maps()
    configs = list(
        OrganizationContentConfig.objects.select_related("organization")
        .filter(daily_discovery_enabled=True)
        .order_by("daily_discovery_priority", "organization__domain")
    )

    max_targets = get_daily_discovery_max_targets()
    overflow = max(0, len(configs) - max_targets)
    if overflow:
        logger.warning(
            "Daily discovery has %s enabled organizations, exceeding the supported maximum of %s. "
            "Only the first %s by priority will be scheduled.",
            len(configs),
            max_targets,
            max_targets,
        )
        configs = configs[:max_targets]

    targets: List[DailyDiscoveryTarget] = []
    skipped_missing_owner = 0
    skipped_missing_prereqs = 0

    for config in configs:
        owner = infer_daily_discovery_owner(config=config, owner_maps=owner_maps)
        owner = _upsert_inferred_owner(config, owner)
        if not owner:
            skipped_missing_owner += 1
            continue
        if not _config_has_research_prereqs(config):
            skipped_missing_prereqs += 1
            continue
        domain = normalize_domain(getattr(config.organization, "domain", ""))
        if not domain:
            skipped_missing_prereqs += 1
            continue
        targets.append(
            DailyDiscoveryTarget(
                slack_user_id=owner,
                domain=domain,
                priority=int(config.daily_discovery_priority or 0),
            )
        )

    return targets, {
        "skipped_missing_owner": skipped_missing_owner,
        "skipped_missing_prereqs": skipped_missing_prereqs,
        "overflow": overflow,
    }


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


def expire_previous_day_open_dispatches(*, now=None) -> int:
    now = now or timezone.now()
    current_local_date = _schedule_local_now(now).date()
    stale = ScheduledDiscoveryDispatch.objects.filter(
        trigger_source="daily_scheduler",
        local_date__lt=current_local_date,
        state__in=list(OPEN_DISPATCH_STATES),
    )
    return stale.update(
        state=ScheduledDiscoveryDispatchState.EXPIRED,
        last_error="Expired when the next scheduled discovery day began.",
    )


def due_daily_discovery_targets(*, now=None) -> List[DailyDiscoveryTarget]:
    local_now = _schedule_local_now(now)
    if local_now.time() < _schedule_start_time():
        return []
    targets, _stats = _eligible_daily_discovery_targets()
    return targets


def _build_client_request_id(*, slack_user_id: str, domain: str, local_date: date) -> str:
    return f"scheduled-daily:{normalize_domain(domain)}:{slack_user_id}:{local_date.isoformat()}"


def build_daily_discovery_schedule(*, now=None) -> dict:
    now = now or timezone.now()
    local_now = _schedule_local_now(now)
    local_date = local_now.date()

    if local_now.time() < _schedule_start_time():
        return {
            "status": "skipped",
            "reason": "before_schedule_start",
            "local_date": local_date.isoformat(),
        }

    existing_count = ScheduledDiscoveryDispatch.objects.filter(
        trigger_source="daily_scheduler",
        local_date=local_date,
    ).count()
    if existing_count:
        return {
            "status": "skipped",
            "reason": "schedule_exists",
            "local_date": local_date.isoformat(),
            "existing_count": existing_count,
        }

    targets, stats = _eligible_daily_discovery_targets()
    if not targets:
        return {
            "status": "skipped",
            "reason": "no_eligible_targets",
            "local_date": local_date.isoformat(),
            **stats,
        }

    created_count = 0
    with transaction.atomic():
        if ScheduledDiscoveryDispatch.objects.select_for_update().filter(
            trigger_source="daily_scheduler",
            local_date=local_date,
        ).exists():
            existing_count = ScheduledDiscoveryDispatch.objects.filter(
                trigger_source="daily_scheduler",
                local_date=local_date,
            ).count()
            return {
                "status": "skipped",
                "reason": "schedule_exists",
                "local_date": local_date.isoformat(),
                "existing_count": existing_count,
                **stats,
            }

        ScheduledDiscoveryDispatch.objects.bulk_create(
            [
                ScheduledDiscoveryDispatch(
                    slack_user_id=target.slack_user_id,
                    domain=target.domain,
                    timezone=get_daily_discovery_schedule_timezone(),
                    local_date=local_date,
                    scheduled_for_at=_schedule_slot_datetime(local_date=local_date, slot_index=index),
                    slot_index=index,
                    trigger_source="daily_scheduler",
                    state=ScheduledDiscoveryDispatchState.SCHEDULED,
                )
                for index, target in enumerate(targets)
            ]
        )
        created_count = len(targets)

    return {
        "status": "scheduled",
        "created": created_count,
        "local_date": local_date.isoformat(),
        **stats,
    }


def _dispatch_existing_scheduled_record(
    *,
    dispatch_id: int,
    now=None,
    force: bool = False,
) -> dict:
    now = now or timezone.now()

    with transaction.atomic():
        dispatch = ScheduledDiscoveryDispatch.objects.select_for_update().get(pk=dispatch_id)
        if dispatch.state != ScheduledDiscoveryDispatchState.SCHEDULED:
            if not force:
                return {
                    "status": "skipped",
                    "reason": "dispatch_not_scheduled",
                    "dispatch_id": dispatch.id,
                    "state": dispatch.state,
                }
            if dispatch.state in {
                ScheduledDiscoveryDispatchState.QUEUED,
                ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
            }:
                return {
                    "status": "skipped",
                    "reason": "dispatch_already_open",
                    "dispatch_id": dispatch.id,
                    "state": dispatch.state,
                }
            dispatch.state = ScheduledDiscoveryDispatchState.SCHEDULED

        dispatch.state = ScheduledDiscoveryDispatchState.QUEUED
        dispatch.timezone = get_daily_discovery_schedule_timezone()
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
        "domain": dispatch.domain,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        "client_request_id": _build_client_request_id(
            slack_user_id=dispatch.slack_user_id,
            domain=dispatch.domain,
            local_date=dispatch.local_date,
        ),
        "trigger_source": SCHEDULED_DAILY_TRIGGER_SOURCE,
        "scheduled_local_date": dispatch.local_date.isoformat(),
        "scheduled_timezone": dispatch.timezone,
        "scheduled_slot_index": dispatch.slot_index,
        "scheduled_for_at": dispatch.scheduled_for_at.isoformat() if dispatch.scheduled_for_at else "",
        "scheduled_channel_name": get_daily_discovery_schedule_channel_name(),
    }

    try:
        result = trigger_article_generation(dispatch.slack_user_id, article_request)
        content_factory_job_id = str(result.get("job_id") or result.get("run_id") or "").strip()
        update_fields = ["timezone", "updated_at"]
        refreshed_dispatch = ScheduledDiscoveryDispatch.objects.get(pk=dispatch.id)
        refreshed_dispatch.timezone = get_daily_discovery_schedule_timezone()
        if content_factory_job_id and refreshed_dispatch.content_factory_job_id != content_factory_job_id:
            refreshed_dispatch.content_factory_job_id = content_factory_job_id
            update_fields.append("content_factory_job_id")
        refreshed_dispatch.save(update_fields=update_fields)
        return {
            "status": "queued",
            "dispatch_id": refreshed_dispatch.id,
            "job_id": content_factory_job_id,
            "state": refreshed_dispatch.state,
            "domain": refreshed_dispatch.domain,
            "slack_user_id": refreshed_dispatch.slack_user_id,
            "local_date": refreshed_dispatch.local_date.isoformat(),
            "slot_index": refreshed_dispatch.slot_index,
        }
    except Exception as exc:
        ScheduledDiscoveryDispatch.objects.filter(pk=dispatch.id).update(
            state=ScheduledDiscoveryDispatchState.FAILED,
            last_error=str(exc),
        )
        return {
            "status": "failed",
            "dispatch_id": dispatch.id,
            "domain": dispatch.domain,
            "slack_user_id": dispatch.slack_user_id,
            "error": str(exc),
        }


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
    del timezone_name
    normalized_domain = normalize_domain(domain)
    normalized_slack_user_id = str(slack_user_id or "").strip()
    if not normalized_domain or not normalized_slack_user_id:
        return {"status": "skipped", "reason": "missing_target_fields"}

    config = (
        OrganizationContentConfig.objects.select_related("organization")
        .filter(organization__domain=normalized_domain)
        .first()
    )
    if not _config_has_research_prereqs(config):
        return {"status": "skipped", "reason": "missing_research_prereqs", "domain": normalized_domain}

    owner = infer_daily_discovery_owner(
        domain=normalized_domain,
        connected_slack_user_id=normalized_slack_user_id,
        config=config,
    )
    if owner and owner != normalized_slack_user_id:
        return {
            "status": "skipped",
            "reason": "owner_mismatch",
            "domain": normalized_domain,
            "expected_owner": owner,
        }

    resolved_local_date = local_date or _schedule_local_now(now).date()

    with transaction.atomic():
        dispatch, created = ScheduledDiscoveryDispatch.objects.select_for_update().get_or_create(
            slack_user_id=normalized_slack_user_id,
            domain=normalized_domain,
            local_date=resolved_local_date,
            trigger_source="daily_scheduler",
            defaults={
                "timezone": get_daily_discovery_schedule_timezone(),
                "scheduled_for_at": now,
                "slot_index": 0,
                "state": ScheduledDiscoveryDispatchState.SCHEDULED,
            },
        )

        if not created and not force:
            return {
                "status": "skipped",
                "reason": "dispatch_already_exists",
                "dispatch_id": dispatch.id,
                "state": dispatch.state,
            }

        if not created and dispatch.state in {
            ScheduledDiscoveryDispatchState.QUEUED,
            ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
        }:
            return {
                "status": "skipped",
                "reason": "dispatch_already_open",
                "dispatch_id": dispatch.id,
                "state": dispatch.state,
            }

        if not created:
            dispatch.state = ScheduledDiscoveryDispatchState.SCHEDULED
            dispatch.timezone = get_daily_discovery_schedule_timezone()
            dispatch.scheduled_for_at = now
            dispatch.slot_index = 0
            dispatch.last_error = ""
            dispatch.content_factory_job_id = ""
            dispatch.slack_channel_id = ""
            dispatch.slack_message_ts = ""
            dispatch.slack_thread_ts = ""
            dispatch.save(
                update_fields=[
                    "state",
                    "timezone",
                    "scheduled_for_at",
                    "slot_index",
                    "last_error",
                    "content_factory_job_id",
                    "slack_channel_id",
                    "slack_message_ts",
                    "slack_thread_ts",
                    "updated_at",
                ]
            )

    result = _dispatch_existing_scheduled_record(dispatch_id=dispatch.id, now=now, force=True)
    if not created and result.get("status") == "queued":
        result["replayed"] = True
    return result


def dispatch_due_scheduled_discoveries(*, now=None, limit: int = 1) -> List[dict]:
    now = now or timezone.now()
    due_ids = list(
        ScheduledDiscoveryDispatch.objects.filter(
            trigger_source="daily_scheduler",
            state=ScheduledDiscoveryDispatchState.SCHEDULED,
            scheduled_for_at__lte=now,
        )
        .order_by("scheduled_for_at", "slot_index", "created_at")
        .values_list("id", flat=True)[: max(1, limit)]
    )
    return [_dispatch_existing_scheduled_record(dispatch_id=dispatch_id, now=now) for dispatch_id in due_ids]


def run_daily_discovery_scheduler(*, now=None) -> dict:
    now = now or timezone.now()
    expired = expire_previous_day_open_dispatches(now=now)
    timed_out = expire_stale_queued_dispatches(now=now)
    schedule_result = build_daily_discovery_schedule(now=now)
    dispatch_results = dispatch_due_scheduled_discoveries(now=now, limit=1)
    return {
        "status": "ok",
        "expired_dispatches": expired,
        "timed_out_dispatches": timed_out,
        "schedule_result": schedule_result,
        "queued": sum(1 for result in dispatch_results if result.get("status") == "queued"),
        "skipped": sum(1 for result in dispatch_results if result.get("status") == "skipped"),
        "failed": sum(1 for result in dispatch_results if result.get("status") == "failed"),
        "results": dispatch_results,
    }


def mark_scheduled_dispatch_topic_selection_sent(
    *,
    job_id: str,
    slack_channel_id: str = "",
    slack_message_ts: str = "",
    slack_thread_ts: str = "",
) -> Optional[ScheduledDiscoveryDispatch]:
    dispatch = ScheduledDiscoveryDispatch.objects.filter(content_factory_job_id=job_id).first()
    if not dispatch:
        return None

    dispatch.state = ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT
    if slack_channel_id:
        dispatch.slack_channel_id = slack_channel_id
    if slack_message_ts:
        dispatch.slack_message_ts = slack_message_ts
    if slack_thread_ts:
        dispatch.slack_thread_ts = slack_thread_ts
    dispatch.save(
        update_fields=[
            "state",
            "slack_channel_id",
            "slack_message_ts",
            "slack_thread_ts",
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
