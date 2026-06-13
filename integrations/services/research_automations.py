from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone as dt_timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from content_factory.models import (
    AutomationRun,
    AutomationRunStatus,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from integrations.services.article_generation import (
    CONTENT_FACTORY_REQUEST_SOURCE,
    _build_content_factory_headers,
    _get_content_factory_base_url,
    _post_content_factory_queue_request,
)
from integrations.services.notification_adapters import notification_context_for_run
from integrations.utils import normalize_domain
from organizations.models import Organization


logger = logging.getLogger(__name__)

DEFAULT_RESEARCH_AUTOMATION_TIMES = ["08:00"]
DEFAULT_RESEARCH_AUTOMATION_TWICE_DAILY_TIMES = ["08:00", "15:30"]
SCHEDULE_LOOKAHEAD_LIMIT = 500


def _coerce_timezone(value: str) -> str:
    candidate = str(value or "").strip() or "Australia/Melbourne"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return "Australia/Melbourne"
    return candidate


def _parse_local_time(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour, minute = text.split(":", 1)
        return time(hour=int(hour), minute=int(minute[:2]))
    except (TypeError, ValueError):
        return None


def normalized_send_times(automation: ResearchAutomation) -> list[time]:
    raw_times = automation.local_send_times
    if not isinstance(raw_times, list) or not raw_times:
        raw_times = (
            DEFAULT_RESEARCH_AUTOMATION_TWICE_DAILY_TIMES
            if int(automation.frequency_per_day or 1) >= 2
            else DEFAULT_RESEARCH_AUTOMATION_TIMES
        )
    parsed = sorted({parsed for item in raw_times if (parsed := _parse_local_time(item))})
    if not parsed:
        parsed = [_parse_local_time(DEFAULT_RESEARCH_AUTOMATION_TIMES[0])]
    frequency = max(1, min(int(automation.frequency_per_day or 1), 2))
    return [item for item in parsed if item is not None][:frequency]


def due_slots_for_automation(
    automation: ResearchAutomation,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    current = now or timezone.now()
    timezone_name = _coerce_timezone(automation.timezone)
    local_now = current.astimezone(ZoneInfo(timezone_name))
    slots: list[dict[str, Any]] = []
    for slot_index, local_time in enumerate(normalized_send_times(automation)):
        local_dt = datetime.combine(local_now.date(), local_time, tzinfo=ZoneInfo(timezone_name))
        scheduled_for_at = local_dt.astimezone(dt_timezone.utc)
        if scheduled_for_at <= current:
            slots.append(
                {
                    "local_date": local_now.date(),
                    "slot_index": slot_index,
                    "local_time": local_time.strftime("%H:%M"),
                    "scheduled_for_at": scheduled_for_at,
                    "timezone": timezone_name,
                }
            )
    return slots


def automation_run_idempotency_key(
    *,
    automation_id: str,
    local_date: date,
    slot_index: int,
) -> str:
    return f"research-automation:{automation_id}:{local_date.isoformat()}:{slot_index}"


def ensure_due_automation_runs(*, now: Optional[datetime] = None, limit: int = SCHEDULE_LOOKAHEAD_LIMIT) -> list[AutomationRun]:
    current = now or timezone.now()
    created_or_existing: list[AutomationRun] = []
    automations = (
        ResearchAutomation.objects.select_related(
            "organization",
            "user",
            "notification_channel",
        )
        .filter(status=ResearchAutomationStatus.ACTIVE)
        # Deliveries fan out to every active org channel, so a run is due as
        # long as any channel is consented — even if the primary opted out.
        .filter(
            Exists(
                NotificationChannel.objects.filter(
                    organization_id=OuterRef("organization_id"),
                    consent_state=NotificationConsentState.ACTIVE,
                )
            )
        )
        .order_by("created_at")[: max(1, limit)]
    )
    for automation in automations:
        for slot in due_slots_for_automation(automation, now=current):
            key = automation_run_idempotency_key(
                automation_id=str(automation.id),
                local_date=slot["local_date"],
                slot_index=slot["slot_index"],
            )
            try:
                with transaction.atomic():
                    run, _created = AutomationRun.objects.select_for_update().get_or_create(
                        automation=automation,
                        local_date=slot["local_date"],
                        slot_index=slot["slot_index"],
                        defaults={
                            "scheduled_for_at": slot["scheduled_for_at"],
                            "status": AutomationRunStatus.SCHEDULED,
                            "idempotency_key": key,
                            "request_payload": {
                                "timezone": slot["timezone"],
                                "local_time": slot["local_time"],
                            },
                        },
                    )
                    if run.scheduled_for_at != slot["scheduled_for_at"]:
                        run.scheduled_for_at = slot["scheduled_for_at"]
                        run.save(update_fields=["scheduled_for_at", "updated_at"])
                    created_or_existing.append(run)
            except IntegrityError:
                existing = AutomationRun.objects.filter(idempotency_key=key).first()
                if existing:
                    created_or_existing.append(existing)
    return created_or_existing


def _discovery_payload_for_run(run: AutomationRun) -> dict[str, Any]:
    channel = run.automation.notification_channel
    organization = run.automation.organization
    domain = normalize_domain(organization.domain)
    payload = {
        "domain": domain,
        "request_source": CONTENT_FACTORY_REQUEST_SOURCE,
        "notification_context": notification_context_for_run(run),
    }
    slack_route_id = ""
    if channel.channel_type == NotificationChannelType.SLACK:
        slack_route_id = channel.route_id
    else:
        slack_channel = (
            NotificationChannel.objects.filter(
                organization=organization,
                channel_type=NotificationChannelType.SLACK,
                consent_state=NotificationConsentState.ACTIVE,
            )
            .order_by("created_at")
            .first()
        )
        if slack_channel:
            slack_route_id = slack_channel.route_id
    if slack_route_id:
        payload["slack_user_id"] = slack_route_id
    if channel.user and channel.user.email:
        payload["user_email"] = channel.user.email
        payload["recipient_user_id"] = str(channel.user_id)
    return payload


def dispatch_automation_run(run_id: str) -> dict[str, Any]:
    with transaction.atomic():
        run = (
            AutomationRun.objects.select_for_update()
            .select_related(
                "automation",
                "automation__organization",
                "automation__user",
                "automation__notification_channel",
                "automation__notification_channel__user",
            )
            .get(id=run_id)
        )
        if run.status != AutomationRunStatus.SCHEDULED:
            return {"status": "skipped", "reason": "run_not_scheduled", "automation_run_id": str(run.id)}
        run.status = AutomationRunStatus.QUEUED
        run.last_error = ""
        run.save(update_fields=["status", "last_error", "updated_at"])

    payload = _discovery_payload_for_run(run)
    endpoint = f"{_get_content_factory_base_url().rstrip('/')}/api/runs/discovery"
    try:
        response = _post_content_factory_queue_request(
            endpoint,
            payload=payload,
            headers=_build_content_factory_headers(),
            operation="queue_research_automation_discovery",
            domain=payload.get("domain"),
        )
        if response.status_code not in {200, 202}:
            error = f"Content Factory returned {response.status_code}: {response.text}"
            AutomationRun.objects.filter(pk=run.id).update(
                status=AutomationRunStatus.FAILED,
                last_error=error,
            )
            return {"status": "failed", "automation_run_id": str(run.id), "error": error}
        data = response.json()
        content_factory_run_id = str(data.get("job_id") or data.get("run_id") or data.get("task_id") or "").strip()
        AutomationRun.objects.filter(pk=run.id).update(
            status=AutomationRunStatus.QUEUED,
            content_factory_run_id=content_factory_run_id,
            request_payload=payload,
            last_error="",
        )
        ResearchAutomation.objects.filter(pk=run.automation_id).update(last_scheduled_for_at=run.scheduled_for_at)
        return {
            "status": "queued",
            "automation_run_id": str(run.id),
            "content_factory_run_id": content_factory_run_id,
        }
    except Exception as exc:
        logger.warning("Failed to dispatch research automation run %s: %s", run.id, exc)
        AutomationRun.objects.filter(pk=run.id).update(
            status=AutomationRunStatus.FAILED,
            last_error=str(exc),
        )
        return {"status": "failed", "automation_run_id": str(run.id), "error": str(exc)}


def dispatch_due_automation_runs(*, now: Optional[datetime] = None, limit: int = 20) -> list[dict[str, Any]]:
    current = now or timezone.now()
    due_ids = list(
        AutomationRun.objects.filter(
            status=AutomationRunStatus.SCHEDULED,
            scheduled_for_at__lte=current,
        )
        .order_by("scheduled_for_at", "created_at")
        .values_list("id", flat=True)[: max(1, limit)]
    )
    return [dispatch_automation_run(str(run_id)) for run_id in due_ids]


def run_research_automation_scheduler(*, now: Optional[datetime] = None, limit: int = 20) -> dict[str, Any]:
    current = now or timezone.now()
    ensured = ensure_due_automation_runs(now=current)
    results = dispatch_due_automation_runs(now=current, limit=limit)
    return {
        "status": "ok",
        "ensured": len(ensured),
        "queued": sum(1 for result in results if result.get("status") == "queued"),
        "failed": sum(1 for result in results if result.get("status") == "failed"),
        "skipped": sum(1 for result in results if result.get("status") == "skipped"),
        "results": results,
    }


def upsert_notification_channel(
    *,
    organization: Organization,
    channel_type: str,
    route_id: str,
    user=None,
    consent_state: str = NotificationConsentState.PENDING,
    display_name: str = "",
    provider_metadata: Optional[dict[str, Any]] = None,
) -> NotificationChannel:
    channel, _created = NotificationChannel.objects.update_or_create(
        organization=organization,
        channel_type=channel_type,
        route_id=str(route_id or "").strip(),
        defaults={
            "user": user,
            "consent_state": consent_state,
            "display_name": display_name,
            "provider_metadata": provider_metadata or {},
            **({"verified_at": timezone.now()} if consent_state == NotificationConsentState.ACTIVE else {}),
        },
    )
    return channel


def create_or_update_research_automation(
    *,
    domain: str,
    channel_type: str,
    route_id: str,
    user=None,
    timezone_name: str = "Australia/Melbourne",
    frequency_per_day: int = 1,
    local_send_times: Optional[Iterable[str]] = None,
    consent_state: str = NotificationConsentState.PENDING,
    name: str = "",
) -> ResearchAutomation:
    normalized_domain = normalize_domain(domain)
    organization = Organization.objects.get(domain=normalized_domain)
    channel = upsert_notification_channel(
        organization=organization,
        channel_type=channel_type,
        route_id=route_id,
        user=user,
        consent_state=consent_state,
    )
    automation, _created = ResearchAutomation.objects.update_or_create(
        organization=organization,
        notification_channel=channel,
        defaults={
            "user": user,
            "name": name,
            "timezone": _coerce_timezone(timezone_name),
            "frequency_per_day": max(1, min(int(frequency_per_day or 1), 2)),
            "local_send_times": list(local_send_times or []),
            "status": ResearchAutomationStatus.ACTIVE,
        },
    )
    return automation
