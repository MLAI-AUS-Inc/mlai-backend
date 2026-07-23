from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
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
from content_factory.billing import get_content_factory_ai_agent_required_points
from integrations.services.article_generation import (
    CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
    CONTENT_FACTORY_REQUEST_SOURCE,
    InsufficientRooPointsError,
    _build_content_factory_headers,
    _content_factory_authorization_payload,
    _get_content_factory_base_url,
    _post_content_factory_queue_request,
    _require_content_factory_ai_agent_points,
    _store_job_tracking_record,
)
from integrations.services.notification_adapters import (
    automation_billing_actor_slack_id,
    notification_context_for_run,
)
from integrations.utils import normalize_domain
from organizations.models import Organization


logger = logging.getLogger(__name__)

DEFAULT_RESEARCH_AUTOMATION_TIMES = ["08:00"]
DEFAULT_RESEARCH_AUTOMATION_TWICE_DAILY_TIMES = ["08:00", "15:30"]
SCHEDULE_LOOKAHEAD_LIMIT = 500
# Runs wait on content-factory callbacks (discovery ~minutes, article ~tens of
# minutes). A lost callback — e.g. the web container recreated mid-flight, as
# happened on 2026-07-07 — leaves a run wedged in QUEUED/GENERATING forever with
# no retry. Fail anything in a machine-waiting state past this window. Excludes
# TOPIC_SELECTION_SENT / DELIVERY_MODE_REQUIRED, which legitimately wait on a human.
STUCK_RUN_TIMEOUT_SECONDS = 3 * 60 * 60
# Manual "Run today now" runs live in a dedicated slot namespace so they can never
# collide with the scheduled slots (enumerate(send_times) -> 0..n-1).
MANUAL_SLOT_BASE = 100


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
        # Daily reminders present three topics; content-factory defaults to 4
        # when unset. Must match the WhatsApp topic template's title slots.
        "requested_topic_count": 3,
    }
    actor_slack_id = automation_billing_actor_slack_id(run.automation)
    if actor_slack_id:
        # Roo-points billing actor (wallet owner). Distinct from the Slack
        # *delivery* route below: a WhatsApp/email automation still bills the
        # founder's wallet. content-factory resolves the actor as
        # requested_by_slack_user_id -> slack_user_id.
        payload["requested_by_slack_user_id"] = actor_slack_id
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
        # of=("self",) locks only the AutomationRun row. The nullable
        # select_related hops (automation.user, notification_channel.user)
        # render as LEFT OUTER JOINs, and Postgres rejects FOR UPDATE on the
        # nullable side of an outer join — an unqualified select_for_update()
        # raises NotSupportedError the moment a run becomes dispatchable.
        run = (
            AutomationRun.objects.select_for_update(of=("self",))
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
    domain = payload.get("domain") or ""
    # Browsing topics is free; the Roo-points charge is deferred to topic
    # approval (confirm_topic -> _charge_deferred_discovery_job_if_needed).
    # Free-listed domains skip billing entirely and keep the bare-dispatch path.
    paying_domain = get_content_factory_ai_agent_required_points(domain) > 0
    endpoint = f"{_get_content_factory_base_url().rstrip('/')}/api/runs/discovery"
    try:
        if paying_domain:
            actor_slack_id = str(payload.get("requested_by_slack_user_id") or "").strip()
            if not actor_slack_id:
                # No wallet owner on the channel: fail with a clear reason rather
                # than let content-factory return an opaque ROO_POINTS_UNAVAILABLE.
                AutomationRun.objects.filter(pk=run.id).update(
                    status=AutomationRunStatus.FAILED,
                    last_error=(
                        "billing_identity_missing: no Slack-linked user on the "
                        "automation channel; cannot charge Roo points for a paid domain."
                    ),
                )
                return {
                    "status": "failed",
                    "automation_run_id": str(run.id),
                    "error": "billing_identity_missing",
                }
            # Gate on balance (raises InsufficientRooPointsError below), then stamp
            # a deferred authorization so content-factory admits the discovery.
            _gated_user, gated_balance = _require_content_factory_ai_agent_points(
                slack_user_id=actor_slack_id,
                article_request=payload,
                resolved_domain=domain,
                action="topic_discovery",
            )
            payload.update(
                _content_factory_authorization_payload(
                    resolved_domain=domain,
                    action="topic_discovery",
                    cost_points=0,
                    billing_status=CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
                    current_balance=gated_balance,
                )
            )

        response = _post_content_factory_queue_request(
            endpoint,
            payload=payload,
            headers=_build_content_factory_headers(),
            operation="queue_research_automation_discovery",
            domain=domain,
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
        if paying_domain and content_factory_run_id:
            # Track the discovery job with a deferred hold so topic approval can
            # charge the founder's wallet. The actor rides in request_meta
            # (requested_by_slack_user_id) for _charge_deferred_discovery_job.
            _store_job_tracking_record(
                content_factory_run_id,
                domain=domain,
                slack_user_id="",
                request_meta=payload,
                default_status="queued",
                client_request_id=run.idempotency_key,
                billing_source_job_id=content_factory_run_id,
                billing_amount=0,
                billing_status=CONTENT_FACTORY_BILLING_STATUS_DEFERRED,
            )
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
    except InsufficientRooPointsError as exc:
        logger.info("Research automation run %s blocked on Roo points: %s", run.id, exc)
        AutomationRun.objects.filter(pk=run.id).update(
            status=AutomationRunStatus.FAILED,
            last_error=f"insufficient_roo_points: {exc}"[:1000],
        )
        return {"status": "failed", "automation_run_id": str(run.id), "error": "insufficient_roo_points"}
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


def start_manual_automation_run(
    organization,
    *,
    requested_by_user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Start an on-demand ("Run today now") research run for an org's automation.

    Creates a manual AutomationRun in the MANUAL_SLOT_BASE namespace (so it never
    collides with the scheduled 0..n-1 slots or the already-consumed 8am slot) and
    dispatches it through the exact same path as the daily send — same discovery,
    billing, top-3 topics, fan-out to enabled channels, buttons, and watchdog.

    Reuses an in-flight manual run (discovery still running, topics not yet sent) so
    an impatient double-click doesn't spend a second content-factory discovery.
    Returns the dispatch_automation_run result dict, or a sentinel status:
    "no_automation" / "no_delivery_channels" / "reused".
    """
    current = now or timezone.now()
    automation = (
        ResearchAutomation.objects.filter(
            organization=organization, status=ResearchAutomationStatus.ACTIVE
        )
        .order_by("created_at")
        .first()
    )
    if automation is None:
        return {"status": "no_automation"}

    has_target = NotificationChannel.objects.filter(
        organization=organization,
        consent_state=NotificationConsentState.ACTIVE,
        delivery_enabled=True,
    ).exists()
    if not has_target:
        return {"status": "no_delivery_channels"}

    timezone_name = _coerce_timezone(automation.timezone)
    local_date = current.astimezone(ZoneInfo(timezone_name)).date()

    in_flight = (
        AutomationRun.objects.filter(
            automation=automation,
            local_date=local_date,
            slot_index__gte=MANUAL_SLOT_BASE,
            status__in=[AutomationRunStatus.SCHEDULED, AutomationRunStatus.QUEUED],
        )
        .order_by("-slot_index")
        .first()
    )
    if in_flight is not None:
        return {
            "status": "reused",
            "automation_run_id": str(in_flight.id),
            "run_status": in_flight.status,
        }

    last_manual_slot = (
        AutomationRun.objects.filter(
            automation=automation,
            local_date=local_date,
            slot_index__gte=MANUAL_SLOT_BASE,
        )
        .order_by("-slot_index")
        .values_list("slot_index", flat=True)
        .first()
    )
    slot_index = (last_manual_slot + 1) if last_manual_slot is not None else MANUAL_SLOT_BASE
    key = automation_run_idempotency_key(
        automation_id=str(automation.id),
        local_date=local_date,
        slot_index=slot_index,
    )
    try:
        run = AutomationRun.objects.create(
            automation=automation,
            local_date=local_date,
            slot_index=slot_index,
            scheduled_for_at=current,
            status=AutomationRunStatus.SCHEDULED,
            idempotency_key=key,
            request_payload={
                "trigger_source": "founder_tools_run_now",
                "requested_by_user_id": requested_by_user_id,
                "timezone": timezone_name,
            },
        )
    except IntegrityError:
        existing = AutomationRun.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return {
                "status": "reused",
                "automation_run_id": str(existing.id),
                "run_status": existing.status,
            }
        raise

    result = dispatch_automation_run(str(run.id))
    result.setdefault("automation_run_id", str(run.id))
    return result


def fail_stuck_automation_runs(
    *,
    now: Optional[datetime] = None,
    timeout_seconds: int = STUCK_RUN_TIMEOUT_SECONDS,
) -> int:
    """Fail runs wedged in a machine-waiting state past the timeout.

    QUEUED (awaiting the discovery callback) and GENERATING (awaiting the article
    callback) advance only when content-factory calls back. A dropped callback
    strands them with no retry, so they keep an automation looking busy forever.
    Flip them to FAILED so watchers/operators can see them and the automation is
    free to schedule its next slot. User-waiting states are intentionally left
    alone — a founder may approve a topic hours later.
    """
    current = now or timezone.now()
    cutoff = current - timedelta(seconds=max(60, timeout_seconds))
    return (
        AutomationRun.objects.filter(
            status__in=[AutomationRunStatus.QUEUED, AutomationRunStatus.GENERATING],
            updated_at__lt=cutoff,
        ).update(
            status=AutomationRunStatus.FAILED,
            last_error="content_factory_timeout: no terminal callback within the timeout window",
            updated_at=current,
        )
    )


def run_research_automation_scheduler(*, now: Optional[datetime] = None, limit: int = 20) -> dict[str, Any]:
    current = now or timezone.now()
    ensured = ensure_due_automation_runs(now=current)
    results = dispatch_due_automation_runs(now=current, limit=limit)
    failed_stuck = fail_stuck_automation_runs(now=current)
    return {
        "status": "ok",
        "ensured": len(ensured),
        "queued": sum(1 for result in results if result.get("status") == "queued"),
        "failed": sum(1 for result in results if result.get("status") == "failed"),
        "skipped": sum(1 for result in results if result.get("status") == "skipped"),
        "failed_stuck": failed_stuck,
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
