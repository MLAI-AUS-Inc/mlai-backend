"""Bounded, idempotent staff incident rules and privacy-safe alert enrichment."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from workflow_runs.models import ContentFactoryRun
from . import staff_signals
from .founder_journey_alerts import queue_founder_journey_alert, ack_due_at, _safe_details, is_scheduler_run
from .models import OrganizationContentConfig
from .registry_policy import is_registered

logger = logging.getLogger(__name__)
LOCAL = ZoneInfo("Australia/Melbourne")
TERMINAL = {"failed", "blocked", "denied", "stopped", "cancelled"}
GLOBAL_RULES = {"provider_error_rate": 600, "throttle_spike": 3600,
                "refresh_failure_spike": 3600, "github_callback_failures": 3600}
GLOBAL_TTL = 7 * 86400


def mapping(value):
    """Return a JSON object, or an empty one for old nullable receipt fields."""
    return value if isinstance(value, dict) else {}


def failure_code(run):
    """Read the authoritative worker failure without persisting its free text."""
    result = mapping(run.result)
    return result.get("error_code") or mapping(result.get("failure")).get("code")


def open_alert(receipt):
    """Acknowledged and resolved alerts no longer need staff attention."""
    return isinstance(receipt, dict) and not receipt.get("acknowledged_at") and not receipt.get("resolved_at")


def overdue(receipt, now):
    """Whether an open alert passed its Melbourne-business-day acknowledgement time."""
    raw = mapping(receipt.get("details")).get("ack_due_at") if isinstance(receipt, dict) else None
    try:
        moment = parse_datetime(raw) if isinstance(raw, str) else None
    except ValueError:
        moment = None
    return bool(open_alert(receipt) and moment and timezone.is_aware(moment) and moment < now)


def enrich_alert_details(receipt, *, organization, run):
    """Only ids, bounded transition tokens and counters belong in the public alert store."""
    result = mapping(getattr(run, "result", None))
    request = mapping(getattr(run, "run_request", None))
    heal = mapping(result.get("self_heal_v1"))
    transitions = result.get("last_transitions", heal.get("history", []))
    transitions = transitions if isinstance(transitions, list) else []
    details = {"founder_user_id": request.get("roo_points_requested_by_user_id") or organization.billing_user_id,
               "item": "website" if getattr(run, "workflow", "") == "article_system_setup" else "article",
               "code": failure_code(run) if run else None, "run_id": getattr(run, "run_id", None),
               "attempts": heal.get("attempts", 0), "last_transitions": transitions[-3:]}
    return _safe_details(details)


def global_key(rule, window):
    """Stable shared-cache identity for a global rate incident."""
    return f"staff_alerts:global:{rule}:{window}"


def global_alerts(*, now=None):
    """Read at most seven days of global receipts in one cache batch."""
    now = now or timezone.now()
    keys = [global_key(rule, int(now.timestamp()) // seconds - offset)
            for rule, seconds in GLOBAL_RULES.items() for offset in range(GLOBAL_TTL // seconds)]
    return sorted((v for v in cache.get_many(keys).values() if isinstance(v, dict)),
                  key=lambda x: x.get("queued_at", ""), reverse=True)


def acknowledge_global(alert_id, *, by_user):
    """Acknowledge a global cache receipt once; return None for an unknown id."""
    receipt = cache.get(alert_id) if alert_id.startswith("staff_alerts:global:") else None
    if not isinstance(receipt, dict):
        return None
    if not receipt.get("acknowledged_at"):
        lock = alert_id + ":ack-lock"
        if not cache.add(lock, True, 10):
            return cache.get(alert_id)
        try:
            receipt = cache.get(alert_id)
            if receipt and not receipt.get("acknowledged_at"):
                receipt = {**receipt, "acknowledged_at": timezone.now().isoformat(),
                           "acknowledged_by_user_id": by_user.pk}
                cache.set(alert_id, receipt, GLOBAL_TTL)
        finally:
            cache.delete(lock)
    return receipt


def _global(rule, *, now):
    key = global_key(rule, int(now.timestamp()) // GLOBAL_RULES[rule])
    receipt = {"alert_id": key, "alert_reason": rule, "queued_at": now.isoformat(), "state": "unconfigured",
               "run_id": None, "details": {"ack_due_at": ack_due_at(now).isoformat()}}
    added = cache.add(key, receipt, GLOBAL_TTL)
    if added:
        logger.warning("staff_alert rule=%s", rule)
    return int(added)


def _run_reasons(run, now):
    result = mapping(run.result)
    code = failure_code(run)
    reasons = []
    if code and not is_registered(code):
        reasons.append("unregistered_failure_code")
    owner_cancel = result.get("cancellation_reason") in {"owner_cancelled", "draft_deleted", "article_discarded", "superseded"}
    if (run.status in TERMINAL and not owner_cancel and not code) or code == "TERMINAL_CODE_MISSING":
        reasons.append("codeless_terminal")
    if run.status in {"queued", "pending", "starting"} and run.created_at < now - timedelta(minutes=10):
        reasons.append("queue_age")
    causes = []
    if mapping(result.get("strict_pass_v1")).get("claims", 0) and (result.get("draft_ready_v1") or run.status in {"completed", "awaiting_approval", "approval_required"}):
        causes.append("claim_findings_delivered")
    request = mapping(run.run_request)
    if code == "RUN_BUDGET_EXHAUSTED" and (request.get("roo_points_billing_status") in {None, "entitled", "free"}
            or request.get("roo_points_entitled")):
        causes.append("free_budget_exhausted")
    if mapping(result.get("preview_repair_v1")).get("outcome") == "exhausted":
        causes.append("repair_loop_exhausted")
    if causes or mapping(result.get("ladder")).get("exhausted_at") or mapping(result.get("self_heal_v1")).get("ladder_exhausted"):
        reasons.append("our_team_item")
    return reasons + causes


def run_alert_rule_sweep(now=None, limit=None):
    """Evaluate 500 startups with batched queries; write only new incident receipts."""
    if not getattr(settings, "ARTICLES_STAFF_ALERT_RULES_ENABLED", False):
        return {"status": "disabled"}
    now = now or timezone.now()
    limit = max(1, min(int(limit or 500), 5000))
    # A cursor rotates batches so later startups cannot starve behind the first 500.
    cursor_key = "staff_alert_rules:organization_cursor"
    cursor = cache.get(cursor_key, 0)
    qs = OrganizationContentConfig.objects.select_related("organization").order_by("organization_id")
    configs = list(qs.filter(organization_id__gt=cursor)[:limit])
    if not configs:
        configs = list(qs[:limit])
    cache.set(cursor_key, configs[-1].organization_id if len(configs) == limit else 0, 86400)
    by_org = {row.organization_id: row for row in configs}
    day_start = timezone.make_aware(datetime.combine(now.astimezone(LOCAL).date(), time.min), LOCAL)
    runs = list(ContentFactoryRun.objects.filter(organization_id__in=by_org).filter(
        Q(status__in=TERMINAL | {"queued", "pending", "starting"}) | Q(created_at__gte=day_start)).order_by("organization_id", "pk"))
    queued = 0
    setup = {}
    current_day = now.astimezone(LOCAL).date()
    for run in runs:
        if is_scheduler_run(run):
            continue
        row = by_org[run.organization_id]
        receipts = mapping(mapping(row.pillar_strategy).get("ops_alerts"))
        for reason in _run_reasons(run, now):
            from .founder_journey_alerts import founder_journey_alert_id
            key = founder_journey_alert_id(organization_id=run.organization_id, alert_reason=reason, run_id=run.run_id)
            if key not in receipts:
                queue_founder_journey_alert(organization=row.organization, alert_reason=reason, run=run)
                receipts[key] = {"run_id": run.run_id}
                queued += 1
        if run.workflow == "article_system_setup" and run.created_at.astimezone(LOCAL).date() == current_day:
            setup[run.organization_id] = setup.get(run.organization_id, 0) + 1
    for row in configs:
        strategy = mapping(row.pillar_strategy)
        exhausted = mapping(mapping(strategy.get("self_heal")).get("ladder")).get("exhausted_at")
        receipts = mapping(strategy.get("ops_alerts"))
        for reason, fires, slot in (("setup_attempts_spike", setup.get(row.organization_id, 0) > 6, "day-"+str(current_day)),
                                    ("our_team_item", bool(exhausted), None)):
            from .founder_journey_alerts import founder_journey_alert_id
            key = founder_journey_alert_id(organization_id=row.organization_id, alert_reason=reason, slot=slot)
            if fires and key not in receipts:
                queue_founder_journey_alert(organization=row.organization, alert_reason=reason, slot=slot)
                queued += 1
    # Rate rules are global, independent of the rotating organization batch.
    provider_filter = Q(result__error_code__icontains="PROVIDER") | Q(result__error_code__icontains="TEMPORARILY_UNAVAILABLE") | Q(result__error_code__icontains="RATE_LIMIT") | Q(result__error_code__icontains="GATEWAY")
    counts = ContentFactoryRun.objects.filter(updated_at__gte=now-timedelta(minutes=10)).exclude(workflow="island_refresh").aggregate(
        total=Count("pk"), errors=Count("pk", filter=provider_filter))
    hour = timedelta(hours=1)
    refresh = staff_signals.total(staff_signals.REFRESH_FAILED, window=hour, now=now)
    baseline = max(0, staff_signals.total(staff_signals.REFRESH_FAILED, window=timedelta(days=7), now=now) - refresh) / 167
    metrics = {"provider_error_rate": bool(counts["total"]) and counts["errors"] / counts["total"] > .2,
               "throttle_spike": staff_signals.total(staff_signals.FOUNDER_THROTTLED, window=hour, now=now) > 5,
               "refresh_failure_spike": refresh > max(3 * baseline, 0),
               "github_callback_failures": staff_signals.total("github_callback_failed", window=hour, now=now) > 5}
    for rule, fires in metrics.items():
        if fires:
            queued += _global(rule, now=now)
    all_strategies = OrganizationContentConfig.objects.values_list("pillar_strategy", flat=True)
    overdue_count = sum(overdue(r, now) for strategy in all_strategies for r in mapping(mapping(strategy).get("ops_alerts")).values())
    overdue_count += sum(overdue(r, now) for r in global_alerts(now=now))
    if cache.add("staff_alert_overdue:"+str(current_day), True, 2*86400):
        logger.warning("staff_alert_overdue count=%s", overdue_count)
    return {"status": "completed", "organizations": len(configs), "queued": queued, "overdue": overdue_count}


def begin_github_callback(module, request):
    """Count failed P1 callback returns, including early and native redirects, once."""
    flow = module.begin_callback(request)
    if flow is None:
        return None
    counted = bool(flow.response is not None)
    if counted:
        staff_signals.count("github_callback_failed")
    original = flow.redirect
    def redirect(reason):
        nonlocal counted
        if reason != module.CONNECTED and not counted:
            staff_signals.count("github_callback_failed")
            counted = True
        return original(reason)
    flow.redirect = redirect
    return flow
