"""Read-only business-outcome health for connection rollout and reconciliation."""

from collections import Counter
from datetime import timedelta
import re


def summarize_website_runs(rows):
    """Aggregate sanitized outcomes; task success alone never implies readiness."""
    statuses, errors, elapsed = Counter(), Counter(), []
    for row in rows:
        statuses[str(row.get("status") or "unknown")] += 1
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        code = str(row.get("result__error_code") or result.get("error_code") or "")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", code):
            errors[code] += 1
        if row.get("status") in {"completed", "failed", "cancelled", "blocked"}:
            start, end = row.get("created_at"), row.get("updated_at")
            if start is not None and end is not None and end >= start:
                elapsed.append((end - start).total_seconds())
    return {"statuses": dict(sorted(statuses.items())), "failureCodes": dict(sorted(errors.items())),
            "meanLifecycleSeconds": round(sum(elapsed) / len(elapsed), 3) if elapsed else None}


def website_connection_health(*, domain=None, now=None, hours=24, row_limit=2000):
    """Return bounded counts and alerts without exposing templates, tokens or source."""
    from django.db.models import Count, Min, Q
    from django.utils import timezone
    from workflow_runs.models import ContentFactoryRun
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteScanSnapshot, WebsiteTemplateRevision
    from .website_rollout import repository_write_policy
    from .one_click_metrics import one_click_outcomes

    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    connections = WebsiteConnection.objects.all()
    configs = OrganizationContentConfig.objects.exclude(github_repo="")
    runs = ContentFactoryRun.objects.filter(workflow__in=["repo_scan", "content_factory_scan"], created_at__gte=since)
    if domain:
        connections = connections.filter(organization__domain=domain)
        configs = configs.filter(organization__domain=domain)
        runs = runs.filter(organization__domain=domain)
    operations = WebsiteConnectionOperation.objects.filter(connection__in=connections)
    pending = operations.exclude(state__in=["completed", "review_required", "needs_user", "cancelled", "failed", "denied", "deleted", "stopped"])
    oldest = pending.aggregate(value=Min("created_at"))["value"]
    rows = list(runs.order_by("-created_at").values("status", "result__error_code", "created_at", "updated_at")[:row_limit + 1])
    truncated = len(rows) > row_limit
    scans = summarize_website_runs(rows[:row_limit])
    failed = scans["statuses"].get("failed", 0) + scans["statuses"].get("blocked", 0)
    # Owner waits are not abandoned scheduler work. Waiting has its saved goal deadline.
    waiting_rows=list(pending.filter(receipt__failure_class="waiting").values("state","receipt","created_at")[:row_limit+1])
    truncated = truncated or len(waiting_rows) > row_limit
    waiting_overdue=sum(waiting_is_overdue(row,now=now) for row in waiting_rows[:row_limit])
    overdue = pending.filter(Q(receipt__failure_class__isnull=True) | ~Q(receipt__failure_class="waiting")).filter(created_at__lt=now - timedelta(minutes=15)).count() + waiting_overdue
    selected = configs.exclude(website_connection=None)
    canonical_ready = selected.filter(website_connection__state="connected", website_connection__capabilities__publishingReady=True).count()
    # This projection is deliberately read-only: legacy readiness is counted for
    # comparison, never promoted to connection authority or copied into targets.
    legacy_ready = configs.filter(articles_scaffolded=True).count()
    alerts = []
    for code, count in (("repository_scan_failed", failed), ("website_reconciliation_overdue", overdue), ("website_health_report_truncated", int(truncated))):
        if count:
            alerts.append({"code": code, "count": count})
    return {
        "asOf": now.isoformat(), "windowHours": hours,
        "writePolicy": repository_write_policy(domain or ""),
        "connectionStates": {row["state"]: row["count"] for row in connections.values("state").annotate(count=Count("pk"))},
        "legacyConnectionsNeedingAdoption": configs.filter(website_connection=None).count(),
        "shadowReadiness": {"legacyScaffolded": legacy_ready, "canonicalVerified": canonical_ready, "grantsAuthority": False},
        "inventorySnapshots": WebsiteScanSnapshot.objects.filter(connection__in=connections, created_at__gte=since).exclude(detector_version="github_head").count(),
        "quarantinedTemplates": WebsiteTemplateRevision.objects.filter(connection__in=connections, status="quarantined").count(),
        "operations": {"states": {row["state"]: row["count"] for row in operations.values("state").annotate(count=Count("pk"))}, "pending": pending.count(), "overdue": overdue, "oldestPendingSeconds": max(0, int((now - oldest).total_seconds())) if oldest else None},
        "scans": {**scans, "sampleLimit": row_limit, "truncated": truncated}, "alerts": alerts,
        "oneClick": one_click_outcomes(since=since, domain=domain, row_limit=row_limit),
    }


def waiting_is_overdue(row, *, now):
    """A waiting state is overdue only at its actual saved goal/legacy deadline."""
    from . import website_goal as goals
    from django.conf import settings
    if row.get("state")=="needs_user":return False
    receipt=goals.mapping(row.get("receipt"));goal=goals.mapping(receipt.get("goal"))
    valid = goals.valid_goal(goal,goal.get("scope"))
    start=goals.stamp(goal.get("started_at")) if valid else goals.stamp(receipt.get("waiting_started_at") or receipt.get("deployment_wait_started_at") or row.get("created_at"))
    if start is None:return True
    seconds=getattr(settings,"ARTICLES_GOAL_WALL_SECONDS",172800) if valid else 86400
    from .website_doctor import number
    return number(seconds) is None or seconds<=0 or (now-start).total_seconds()>=seconds
