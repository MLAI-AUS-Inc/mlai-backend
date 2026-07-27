from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .connectors.gmail import renew_gmail_watch
from .connectors.registry import connector_registry
from .drive_watch import renew_drive_watch
from .models import (
    MemoryActionStatus,
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemoryConnectionHealthSnapshot,
    MemoryConnectionHealthStatus,
    MemoryConnectionState,
    MemoryDailyCostLedger,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryScopeStatus,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySyncRunStatus,
    MemoryWorkStatus,
)
from .scheduling import (
    provider_freshness_slo_seconds,
    provider_sync_interval_seconds,
    reconciliation_window,
)


TERMINAL_SCHEDULE_STATUSES = frozenset({"completed", "noop", "error"})
CONNECTED_CREDENTIAL_STATUSES = frozenset({"active", "connected", "healthy", "ok"})


def _safe_error(exc) -> str:
    value = " ".join(str(exc or exc.__class__.__name__).split())
    return f"{exc.__class__.__name__}: {value[:300]}"


def _selected_scopes(configuration):
    return list(
        configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).order_by("scope_type", "external_id")
    )


def _renew_watch_for_configuration(configuration, scopes) -> tuple[str, dict]:
    if configuration.provider == "gmail":
        if not str(getattr(settings, "ORG_MEMORY_GMAIL_PUBSUB_TOPIC", "") or "").strip():
            return "disabled", {"renewal_attempted": False}
        watch = renew_gmail_watch(
            configuration,
            [scope.external_id for scope in scopes],
        )
        if watch is None:
            return "disabled", {"renewal_attempted": False}
        return str(watch.status), {
            "renewal_attempted": True,
            "expiration_at": watch.expiration_at.isoformat() if watch.expiration_at else None,
            "last_renewed_at": (
                watch.last_renewed_at.isoformat() if watch.last_renewed_at else None
            ),
        }
    if configuration.provider == "google_drive":
        callback = str(
            getattr(settings, "ORG_MEMORY_DRIVE_WATCH_CALLBACK_URL", "") or ""
        ).strip()
        if not callback:
            return "disabled", {"renewal_attempted": False}
        try:
            watch = renew_drive_watch(configuration)
        except Exception as exc:
            return "error", {
                "renewal_attempted": True,
                "error": _safe_error(exc),
            }
        return str(watch.status), {
            "renewal_attempted": True,
            "expiration_at": watch.expiration_at.isoformat(),
        }
    return "not_applicable", {"renewal_attempted": False}


def _recent_sync_action(configuration, report):
    daily_key = f"daily-reconcile:{report.report_date.isoformat()}"
    daily = configuration.action_requests.filter(
        action=MemoryActionType.SYNC,
        idempotency_key=daily_key,
    ).first()
    if daily is not None:
        return daily
    return (
        configuration.action_requests.filter(
            action=MemoryActionType.SYNC,
            requested_at__gte=report.window_started_at,
            status__in=(
                MemoryActionStatus.PENDING,
                MemoryActionStatus.RUNNING,
                MemoryActionStatus.COMPLETED,
            ),
        )
        .order_by("-requested_at")
        .first()
    )


@transaction.atomic
def _ensure_daily_action(configuration, report, *, now):
    configuration = (
        MemoryConnectionConfiguration.objects.select_for_update()
        .select_related("organization")
        .get(pk=configuration.pk)
    )
    if configuration.lifecycle_state != MemoryConnectionState.ACTIVE:
        return None, "error", "Connection is no longer active."
    if not _selected_scopes(configuration):
        return None, "error", "Select at least one source scope."
    if not connector_registry.enablement(
        configuration.organization,
        configuration.provider,
    )["enabled"]:
        return None, "error", "Enable the provider for this organisation."
    action = _recent_sync_action(configuration, report)
    if action is not None:
        return action, _action_schedule_status(action), ""
    if configuration.action_requests.filter(
        status__in=(MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING)
    ).exists() or configuration.sync_runs.filter(
        status__in=(MemorySyncRunStatus.PENDING, MemorySyncRunStatus.RUNNING)
    ).exists():
        return None, "waiting", "Wait for the active connection action to finish."

    interval = provider_sync_interval_seconds(
        configuration.provider,
        configuration=configuration,
    )
    action = MemorySourceActionRequest.objects.create(
        configuration=configuration,
        action=MemoryActionType.SYNC,
        status=MemoryActionStatus.PENDING,
        request_id=f"memory-daily-{report.report_date.isoformat()}",
        idempotency_key=f"daily-reconcile:{report.report_date.isoformat()}",
    )
    configuration.last_sync_requested_at = now
    configuration.next_scheduled_sync_at = now + timedelta(seconds=interval)
    configuration.save(
        update_fields=(
            "last_sync_requested_at",
            "next_scheduled_sync_at",
            "updated_at",
        )
    )
    MemorySourceAuditEvent.objects.create(
        organization=configuration.organization,
        configuration=configuration,
        event_type="daily_reconciliation_requested",
        request_id=action.request_id,
        metadata={
            "action_id": str(action.pk),
            "report_id": str(report.pk),
            "report_date": report.report_date.isoformat(),
            "interval_seconds": interval,
        },
    )
    return action, "reconciling", ""


def _action_schedule_status(action) -> str:
    if action.status in {MemoryActionStatus.PENDING, MemoryActionStatus.RUNNING}:
        return "reconciling"
    if action.status == MemoryActionStatus.COMPLETED:
        result = dict(action.result_summary or {})
        if not int(result.get("records") or 0) and not int(result.get("removals") or 0):
            return "noop"
        return "completed"
    return "error"


def _connection_counts(configuration, action) -> dict:
    work = configuration.work_items.all()
    counts = {
        "selected_scopes": configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        ).count(),
        "work_pending": work.filter(status=MemoryWorkStatus.PENDING).count(),
        "work_processing": work.filter(status=MemoryWorkStatus.PROCESSING).count(),
        "work_dead": work.filter(status=MemoryWorkStatus.DEAD).count(),
    }
    run = getattr(action, "sync_run", None) if action is not None else None
    if run is not None:
        counts.update(
            {
                "pages_completed": run.pages_completed,
                "records_processed": run.records_processed,
                "removals_processed": run.removals_processed,
            }
        )
    return counts


def _is_catch_up(configuration, *, now, interval_seconds) -> bool:
    if configuration.next_scheduled_sync_at is not None:
        return configuration.next_scheduled_sync_at < now
    if configuration.last_successful_sync_at is not None:
        return (
            now - configuration.last_successful_sync_at
        ).total_seconds() > interval_seconds
    return False


def _refresh_connection_snapshot(configuration, report, *, now):
    snapshot, created = MemoryConnectionHealthSnapshot.objects.get_or_create(
        report=report,
        configuration=configuration,
        defaults={
            "organization": configuration.organization,
            "provider": configuration.provider,
        },
    )
    scopes = _selected_scopes(configuration)
    interval = provider_sync_interval_seconds(
        configuration.provider,
        configuration=configuration,
    )
    catch_up = _is_catch_up(
        configuration,
        now=now,
        interval_seconds=interval,
    )
    existing_details = dict(snapshot.details or {})
    if created or "watch_renewal" not in existing_details:
        provider_enabled = connector_registry.enablement(
            configuration.organization,
            configuration.provider,
        )["enabled"]
        if provider_enabled and scopes:
            watch_status, watch_details = _renew_watch_for_configuration(
                configuration,
                scopes,
            )
        else:
            watch_status, watch_details = "disabled", {
                "renewal_attempted": False,
                "reason": "provider_disabled" if not provider_enabled else "no_selected_scope",
            }
        existing_details["watch_renewal"] = watch_details
    else:
        watch_status = snapshot.watch_status

    action, schedule_status, operator_action = _ensure_daily_action(
        configuration,
        report,
        now=now,
    )
    if action is not None:
        action.refresh_from_db()
        schedule_status = _action_schedule_status(action)
        if schedule_status == "error":
            operator_action = "Repair the failed sync and request a bounded retry."
    configuration.refresh_from_db()
    freshness_slo = provider_freshness_slo_seconds(
        configuration.provider,
        configuration=configuration,
    )
    try:
        provider_health = connector_registry.get(configuration.provider).health(
            configuration
        )
        credential_status = str(provider_health.credential_status or "unknown").lower()
        lag = provider_health.source_lag_seconds
        provider_details = dict(provider_health.details or {})
        provider_error = ""
    except Exception as exc:
        credential_status = "error"
        lag = None
        provider_details = {}
        provider_error = _safe_error(exc)

    freshness_status = (
        "unknown"
        if lag is None
        else "stale"
        if int(lag) > freshness_slo
        else "current"
    )
    if credential_status not in CONNECTED_CREDENTIAL_STATUSES:
        health_status = MemoryConnectionHealthStatus.ERROR
        operator_action = "Repair or re-authorise the provider credential."
    elif schedule_status in {"reconciling", "waiting"}:
        health_status = MemoryConnectionHealthStatus.SYNCING
    elif schedule_status == "error":
        health_status = MemoryConnectionHealthStatus.ERROR
    elif freshness_status != "current":
        health_status = MemoryConnectionHealthStatus.STALE
        operator_action = "Run and verify a successful source reconciliation."
    else:
        health_status = MemoryConnectionHealthStatus.HEALTHY
        operator_action = ""
    if watch_status == "error":
        health_status = MemoryConnectionHealthStatus.ERROR
        operator_action = "Repair the provider watch configuration and renew it."

    existing_details.update(
        {
            "provider_health": provider_details,
            "provider_health_error": provider_error,
        }
    )
    snapshot.organization = configuration.organization
    snapshot.provider = configuration.provider
    snapshot.action_request = action
    snapshot.health_status = health_status
    snapshot.schedule_status = schedule_status
    snapshot.credential_status = credential_status
    snapshot.freshness_status = freshness_status
    snapshot.watch_status = watch_status
    snapshot.provider_interval_seconds = interval
    snapshot.freshness_slo_seconds = freshness_slo
    snapshot.source_lag_seconds = lag
    snapshot.catch_up = catch_up
    snapshot.last_attempted_sync_at = configuration.last_sync_requested_at
    snapshot.last_successful_sync_at = configuration.last_successful_sync_at
    snapshot.operator_action = operator_action
    snapshot.counts = _connection_counts(configuration, action)
    snapshot.details = existing_details
    snapshot.full_clean()
    snapshot.save()
    return snapshot


def _connection_alerts(snapshot) -> list[dict]:
    base = {
        "configuration_id": str(snapshot.configuration_id),
        "provider": snapshot.provider,
    }
    alerts = []
    if snapshot.credential_status not in CONNECTED_CREDENTIAL_STATUSES:
        alerts.append({**base, "code": "credential_unhealthy", "severity": "high"})
    if snapshot.freshness_status in {"stale", "unknown"} and snapshot.schedule_status not in {
        "reconciling",
        "waiting",
    }:
        alerts.append(
            {
                **base,
                "code": "freshness_slo_missed",
                "severity": "high",
                "source_lag_seconds": snapshot.source_lag_seconds,
                "freshness_slo_seconds": snapshot.freshness_slo_seconds,
            }
        )
    if snapshot.watch_status == "error":
        alerts.append({**base, "code": "watch_unhealthy", "severity": "high"})
    if snapshot.schedule_status == "error":
        alerts.append({**base, "code": "daily_sync_failed", "severity": "high"})
    if int((snapshot.counts or {}).get("work_dead") or 0):
        alerts.append(
            {
                **base,
                "code": "dead_work_present",
                "severity": "high",
                "count": int(snapshot.counts["work_dead"]),
            }
        )
    return alerts


def _cost_summary(organization, report_date) -> tuple[dict, list[dict]]:
    ledger = MemoryDailyCostLedger.objects.filter(
        organization=organization,
        budget_date=report_date,
    ).first()
    ceiling = Decimal(
        str(getattr(settings, "ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD", 0) or 0)
    )
    pricing = {
        "embedding": str(
            getattr(settings, "ORG_MEMORY_EMBEDDING_COST_AUD_PER_MILLION_TOKENS", 0)
            or 0
        ),
        "model_input": str(
            getattr(settings, "ORG_MEMORY_MODEL_INPUT_COST_AUD_PER_MILLION_TOKENS", 0)
            or 0
        ),
        "model_output": str(
            getattr(settings, "ORG_MEMORY_MODEL_OUTPUT_COST_AUD_PER_MILLION_TOKENS", 0)
            or 0
        ),
    }
    pricing_configured = all(Decimal(value) > 0 for value in pricing.values())
    deferred_count = organization.memory_work_items.filter(
        status=MemoryWorkStatus.PENDING,
        last_error__startswith="Daily model cost ",
    ).count()
    summary = {
        "currency": "AUD",
        "ceiling_aud": str(ledger.ceiling_aud if ledger else ceiling),
        "reserved_aud": str(ledger.reserved_aud if ledger else Decimal("0")),
        "consumed_aud": str(ledger.consumed_aud if ledger else Decimal("0")),
        "pricing_configured": pricing_configured,
        "deferred_work": deferred_count,
        "rates_aud_per_million_tokens": pricing,
    }
    alerts = []
    if ceiling > 0 and not pricing_configured:
        alerts.append(
            {
                "code": "cost_pricing_not_configured",
                "severity": "high",
            }
        )
    if ledger and ledger.ceiling_aud > 0 and (
        ledger.reserved_aud + ledger.consumed_aud >= ledger.ceiling_aud
    ):
        alerts.append({"code": "daily_cost_ceiling_reached", "severity": "high"})
    elif deferred_count:
        alerts.append({"code": "daily_cost_work_deferred", "severity": "high", "count": deferred_count})
    return summary, alerts


def _refresh_report(report, *, now):
    snapshots = list(
        report.connection_snapshots.select_related("configuration").order_by(
            "provider", "configuration_id"
        )
    )
    schedule_counts = Counter(row.schedule_status for row in snapshots)
    health_counts = Counter(row.health_status for row in snapshots)
    freshness_counts = Counter(row.freshness_status for row in snapshots)
    alerts = [alert for row in snapshots for alert in _connection_alerts(row)]
    cost, cost_alerts = _cost_summary(report.organization, report.report_date)
    alerts.extend(cost_alerts)
    alert_seconds = max(
        int(
            getattr(
                settings,
                "ORG_MEMORY_DAILY_RECONCILIATION_ALERT_SECONDS",
                3600,
            )
        ),
        1,
    )
    terminal = bool(snapshots) and all(
        row.schedule_status in TERMINAL_SCHEDULE_STATUSES for row in snapshots
    )
    if not terminal and (now - report.started_at).total_seconds() >= alert_seconds:
        alerts.append({"code": "daily_reconciliation_overdue", "severity": "high"})
    if terminal:
        unhealthy = any(
            row.health_status != MemoryConnectionHealthStatus.HEALTHY
            for row in snapshots
        )
        report.status = (
            MemoryDailyReconciliationStatus.DEGRADED
            if unhealthy or alerts
            else MemoryDailyReconciliationStatus.COMPLETED
        )
        report.completed_at = report.completed_at or now
    else:
        report.status = MemoryDailyReconciliationStatus.RUNNING
        report.completed_at = None
    report.summary = {
        "connections": len(snapshots),
        "schedule_status": dict(sorted(schedule_counts.items())),
        "health_status": dict(sorted(health_counts.items())),
        "freshness_status": dict(sorted(freshness_counts.items())),
        "catch_up_connections": sum(int(row.catch_up) for row in snapshots),
        "cost": cost,
    }
    report.alerts = alerts
    report.save(
        update_fields=(
            "status",
            "summary",
            "alerts",
            "completed_at",
            "updated_at",
        )
    )
    return report


def serialize_connection_health_snapshot(snapshot) -> dict:
    return {
        "id": str(snapshot.pk),
        "configuration_id": str(snapshot.configuration_id),
        "provider": snapshot.provider,
        "health_status": snapshot.health_status,
        "schedule_status": snapshot.schedule_status,
        "credential_status": snapshot.credential_status,
        "freshness_status": snapshot.freshness_status,
        "source_lag_seconds": snapshot.source_lag_seconds,
        "freshness_slo_seconds": snapshot.freshness_slo_seconds,
        "provider_interval_seconds": snapshot.provider_interval_seconds,
        "watch_status": snapshot.watch_status,
        "catch_up": snapshot.catch_up,
        "last_attempted_sync_at": snapshot.last_attempted_sync_at,
        "last_successful_sync_at": snapshot.last_successful_sync_at,
        "operator_action": snapshot.operator_action,
        "counts": snapshot.counts,
        "details": snapshot.details,
        "updated_at": snapshot.updated_at,
    }


def serialize_daily_reconciliation_report(report, *, include_connections=True) -> dict:
    payload = {
        "id": str(report.pk),
        "organization_id": report.organization_id,
        "report_date": report.report_date,
        "time_zone": report.time_zone,
        "window_started_at": report.window_started_at,
        "status": report.status,
        "summary": report.summary,
        "alerts": report.alerts,
        "started_at": report.started_at,
        "completed_at": report.completed_at,
        "updated_at": report.updated_at,
    }
    if include_connections:
        payload["connections"] = [
            serialize_connection_health_snapshot(row)
            for row in report.connection_snapshots.order_by(
                "provider", "configuration_id"
            )
        ]
    return payload


def run_daily_reconciliation(*, now=None, organization_id=None, force=False) -> dict:
    from .review_summaries import run_post_reconciliation_artifacts

    now = now or timezone.now()
    window = reconciliation_window(now)
    if not force and not window["due"]:
        return {
            "status": "not_due",
            "report_date": window["report_date"],
            "time_zone": window["time_zone"],
            "reports": [],
        }
    configurations = (
        MemoryConnectionConfiguration.objects.filter(
            lifecycle_state=MemoryConnectionState.ACTIVE,
        )
        .select_related("organization", "external_connection", "google_connection")
        .order_by("organization_id", "provider", "created_at")
    )
    if organization_id is not None:
        configurations = configurations.filter(organization_id=organization_id)
    by_organization = {}
    for configuration in configurations:
        by_organization.setdefault(configuration.organization_id, []).append(configuration)

    reports = []
    for organization_configurations in by_organization.values():
        organization = organization_configurations[0].organization
        report, created = MemoryDailyReconciliationReport.objects.get_or_create(
            organization=organization,
            report_date=window["report_date"],
            defaults={
                "time_zone": window["time_zone"],
                "window_started_at": window["window_started_at"],
                "started_at": now,
            },
        )
        reported_configuration_ids = set(
            report.connection_snapshots.values_list("configuration_id", flat=True)
        )
        current_configuration_ids = {
            row.pk for row in organization_configurations
        }
        if (
            not created
            and not force
            and reported_configuration_ids == current_configuration_ids
            and report.status
            in {
                MemoryDailyReconciliationStatus.COMPLETED,
                MemoryDailyReconciliationStatus.DEGRADED,
            }
        ):
            payload = serialize_daily_reconciliation_report(report)
            payload["derived_artifacts"] = run_post_reconciliation_artifacts(
                report=report
            )
            reports.append(payload)
            continue
        for configuration in organization_configurations:
            _refresh_connection_snapshot(configuration, report, now=now)
        _refresh_report(report, now=now)
        payload = serialize_daily_reconciliation_report(report)
        payload["derived_artifacts"] = run_post_reconciliation_artifacts(
            report=report
        )
        reports.append(payload)
    return {
        "status": "processed",
        "report_date": window["report_date"],
        "time_zone": window["time_zone"],
        "reports": reports,
    }
