from __future__ import annotations

import hashlib
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
from content_factory.billing import (
    CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
    build_roo_points_authorization_payload,
    get_content_factory_research_cost_points,
)
from integrations.services.article_generation import (
    CONTENT_FACTORY_BILLING_STATUS_CHARGED,
    CONTENT_FACTORY_REQUEST_SOURCE,
    InsufficientRooPointsError,
    charge_content_factory_topic_generation_for_user,
    refund_content_factory_topic_generation_for_user,
    _content_factory_balance_for_user,
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



def _scheduled_website_binding(organization):
    """Capture website consent when the scheduled job is created, never on retry."""
    from content_factory.website_connections import contract_for
    config = getattr(organization, "content_config", None)
    if config and config.website_connection_id and config.website_connection.state in {"connected", "paused"}:
        return contract_for(config.website_connection)
    return {}


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
        from .daily_research_policy import pause_if_unanswered
        if pause_if_unanswered(automation.organization, now=current):
            continue
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
                                **_scheduled_website_binding(automation.organization),
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
    from content_factory.website_contract import connection_contract
    original = run.request_payload or {}
    if connection_contract(original):
        payload.update({key: original[key] for key in ("website_connection_id", "connection_generation", "repository_id", "connection_target_id", "github_repo", "app_root", "branch", "expected_source_sha") if key in original})
    from .daily_research_policy import daily_topic_policy
    payload["daily_topic_policy"] = daily_topic_policy(run)
    payload["client_request_id"] = run.idempotency_key
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
    from .daily_research_policy import pause_if_unanswered
    candidate = AutomationRun.objects.select_related("automation__organization").get(id=run_id)
    if candidate.slot_index < MANUAL_SLOT_BASE and pause_if_unanswered(candidate.automation.organization):
        return {"status": "skipped", "reason": "three_unanswered_days", "automation_run_id": str(candidate.id)}
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.automation.organization_id)
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
        if run.automation.status != ResearchAutomationStatus.ACTIVE:
            return {"status": "skipped", "reason": "automation_paused", "automation_run_id": str(run.id)}
        if AutomationRun.objects.filter(automation__organization=run.automation.organization, status=AutomationRunStatus.QUEUED).exclude(pk=run.pk).exists():
            return {"status": "skipped", "reason": "research_already_running", "automation_run_id": str(run.id)}
        run.status = AutomationRunStatus.QUEUED
        run.last_error = ""
        run.save(update_fields=["status", "last_error", "updated_at"])

    payload = _discovery_payload_for_run(run)
    domain = payload.get("domain") or ""
    payload["client_request_id"] = "automation-research:" + hashlib.sha256(
        f"{run.automation.organization_id}:{run.idempotency_key}".encode()).hexdigest()
    paying_domain = get_content_factory_research_cost_points(domain, 3) > 0
    charged_user, charge_ledger, cost_points = None, None, 0
    queue_entered = False
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
            from integrations.services.github_installations import resolve_user_for_actor_id
            charged_user = resolve_user_for_actor_id(actor_slack_id)
            if charged_user is None:
                raise ValueError("billing_identity_missing")
            charged_user, charge_ledger, cost_points = charge_content_factory_topic_generation_for_user(
                user=charged_user, actor_id=actor_slack_id,
                article_request=payload,
                resolved_domain=domain,
            )
            payload.update(
                build_roo_points_authorization_payload(
                    domain=domain,
                    action=CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
                    cost_points=cost_points,
                    required_points=cost_points,
                    billing_status=CONTENT_FACTORY_BILLING_STATUS_CHARGED,
                    current_balance=_content_factory_balance_for_user(charged_user),
                    ledger_id=getattr(charge_ledger, "pk", None),
                )
            )

        from types import SimpleNamespace
        from content_factory.vibe_marketing_views import _queue_content_factory_run, _get_config
        user = charged_user or run.automation.user or run.automation.notification_channel.user
        if user is None:
            raise ValueError("billing_identity_missing")
        context = SimpleNamespace(organization=run.automation.organization, profile=SimpleNamespace(user=user))
        queue_config = _get_config(context.organization)
        queue_entered = True
        remote_run = _queue_content_factory_run(endpoint="discovery", workflow="auto_discovery",
            context=context, config=queue_config, payload=payload,
            billing_refund_context={"kind": CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
                "charged_user": charged_user, "article_request": payload,
                "reason": "Scheduled research could not start."} if charged_user else None)
        content_factory_run_id = str(remote_run.run_id)
        unresolved = bool((remote_run.run_request or {}).get("dispatch_pending_resolution"))
        queue_failed = remote_run.status in {"blocked", "failed", "cancelled"} and not unresolved
        if paying_domain and content_factory_run_id:
            _store_job_tracking_record(
                content_factory_run_id,
                domain=domain,
                slack_user_id="",
                request_meta=payload,
                default_status="queued",
                client_request_id=payload["client_request_id"],
                billing_source_job_id=content_factory_run_id,
                billing_amount=cost_points,
                billing_status="refunded" if queue_failed else CONTENT_FACTORY_BILLING_STATUS_CHARGED,
                billing_ledger_id=getattr(charge_ledger, "pk", None),
            )
        AutomationRun.objects.filter(pk=run.id).update(
            status=AutomationRunStatus.FAILED if queue_failed else AutomationRunStatus.QUEUED,
            content_factory_run_id=content_factory_run_id,
            request_payload=payload,
            last_error=str(remote_run.error or "Research could not start.") if queue_failed else "",
        )
        ResearchAutomation.objects.filter(pk=run.automation_id).update(last_scheduled_for_at=run.scheduled_for_at)
        return {
            "status": "failed" if queue_failed else "queued",
            "automation_run_id": str(run.id),
            "content_factory_run_id": content_factory_run_id,
            **({"error": str(remote_run.error or "dispatch_failed")} if queue_failed else {}),
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
        if charged_user is not None and cost_points > 0 and not queue_entered:
            # The queue helper handles ambiguity and owns refunds after dispatch.
            # Failures before entering it are safe to refund here.
            refund_content_factory_topic_generation_for_user(user=charged_user, actor_id=actor_slack_id,
                article_request=payload, resolved_domain=domain, reason=str(exc))
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
    request_id: Optional[str] = None,
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

    explicit_key = None
    if request_id:
        explicit_key = "manual-research:" + hashlib.sha256(
            f"{organization.pk}:{requested_by_user_id}:{str(request_id)[:200]}".encode()).hexdigest()
        existing = AutomationRun.objects.filter(automation=automation, idempotency_key=explicit_key).first()
        if existing is not None:
            return {"status": "reused", "automation_run_id": str(existing.id), "run_status": existing.status}

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
                **_scheduled_website_binding(automation.organization),
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


def reconcile_automation_research_dispatch(run):
    """Resolve a lost queue response with the same canonical dispatch key."""
    from workflow_runs.models import ContentFactoryRun
    from content_factory.vibe_marketing_views import _resolve_dispatch_token_run, _run_pending_remote_dispatch
    if run.status != AutomationRunStatus.QUEUED or not run.content_factory_run_id:
        return run
    domain = normalize_domain(run.automation.organization.domain)
    local = ContentFactoryRun.objects.filter(run_id=run.content_factory_run_id,
        domain=domain, workflow="auto_discovery").first()
    if local is None or not _run_pending_remote_dispatch(local):
        return run
    resolved = _resolve_dispatch_token_run(local)
    if resolved is None:
        return run
    run.content_factory_run_id = resolved.run_id
    if resolved.status in {"failed", "cancelled"}:
        run.status = AutomationRunStatus.FAILED
        run.last_error = resolved.error or "Research could not start. Your points were refunded."
    run.save(update_fields=["content_factory_run_id", "status", "last_error", "updated_at"])
    return run


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
    from .daily_research_policy import record_engagement
    record_engagement(organization, resume=True)
    automation.refresh_from_db()
    return automation
