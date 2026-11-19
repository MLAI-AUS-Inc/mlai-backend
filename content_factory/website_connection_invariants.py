"""Connection invariant sweep with staff alerts (plan step B22).

Every 18 Nov 2026 connection fault was a state that should never exist and
that nothing noticed: a Ready site still listing ``scan_required`` (Tala
Thrive), a source blocker on a site that was never verified (Studio Narrm,
Pawshare, Examon), a stopped preparation whose code the founder cannot act on
(MLAI), a connection-required failure on a healthy connection (La Vie 5396), an
old open operation on a Ready connection (La Vie ``ecebb832``) and article runs
the system cancelled with no word to the founder (Tala Thrive).

This sweep reads local rows only (no provider calls, no writes besides the
alert receipt) and queues one ``connection_invariant`` staff alert per broken
connection, led by the highest-priority invariant; the others ride along in
its details. The alert slot names the leading invariant and its row, so the
same fault alerts once, and a different fault on the same connection later
alerts once more.

It runs at most once per ``SWEEP_INTERVAL_SECONDS`` and honours
``ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED`` (the always-on stuck kill switch).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

ALERT_REASON = "connection_invariant"
SWEEP_INTERVAL_SECONDS = 3600
THROTTLE_KEY = "website_connection_invariants:last_sweep"
CONNECTION_LIMIT = 200
MAX_ALERTS_PER_SWEEP = 25
HEAD_DRIFT_AFTER = timedelta(minutes=30)
OLD_OPERATION_AFTER = timedelta(days=3)
SYSTEM_STOP_GRACE = timedelta(hours=1)
RUN_LOOKBACK = timedelta(days=14)

# Highest priority first: the first violation found leads the connection's one alert.
INVARIANTS = (
    "ready_with_scan_or_source_blocker",
    "source_blocker_never_verified",
    "connection_required_failure_while_connected",
    "prepare_stopped_on_platform_code",
    "worker_projection_invalid_while_ready",
    "head_drift_without_job",
    "old_open_operation_on_ready",
    "system_stopped_run_without_notice",
)
STALE_BLOCKERS = frozenset({"scan_required", "repository_source_changed"})
# Codes that ask the founder to reconnect; on a connected site they are our fault.
CONNECTION_REQUIRED_CODES = frozenset({"website_connection_required", "callback_contract_missing", "website_not_connected"})
# Classes a stopped preparation must never sit in: the founder has nothing to do.
PLATFORM_CLASSES = frozenset({"transient", "waiting", "repairable"})
OPEN_STATES = ("pending", "running", "blocked", "needs_user")
WORK_ACTIONS = ("workflow", "prepare", "source-reverify", "setup-pr-merge", "worker_followup", "ci-verify",
    "deployment-verify", "superseded-setup-cleanup")
JOB_ACTIONS = ("source-reverify", "prepare")


@dataclass(frozen=True)
class Violation:
    """One broken invariant on one connection; ``subject`` is the row it is about."""

    organization_id: int
    connection_id: str
    invariant: str
    subject: str
    details: dict = field(default_factory=dict)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _blocker_codes(connection):
    return {item.get("code") for item in connection.blockers or [] if isinstance(item, dict)}


def _selected_target(connection, config):
    key = str(getattr(config, "default_publish_target_id", "") or "")
    return connection.targets.filter(generation=connection.generation, target_key=key).first() if key else None


def ready(connection, config) -> bool:
    """What the founder summary calls Ready: connected and verified at the selected target's source."""
    target = _selected_target(connection, config)
    return bool(connection.state == "connected" and connection.verified_sha
        and _mapping(connection.capabilities).get("publishingReady") and target is not None
        and target.verified_at and target.source_sha == connection.verified_sha)


def _never_verified(connection):
    return (not connection.verified_sha and not _mapping(connection.capabilities).get("publishingReady")
        and not connection.targets.filter(generation=connection.generation, verified_at__isnull=False).exists())


def _run_codes(run):
    result = _mapping(run.result)
    codes = {result.get("error_code"), _mapping(result.get("failure")).get("code"),
        _mapping(result.get("callback_rejection")).get("code")}
    codes |= {item.get("code") for item in result.get("authority_refusals") or [] if isinstance(item, dict)}
    return {code for code in codes if isinstance(code, str) and code}


def _registry_class(code):
    from .website_failure_contract import load_registry
    entry = load_registry()["by_code_v2"].get(code)
    return entry["failure_class"] if entry else None


def _operation_code(op):
    receipt = _mapping(op.receipt)
    for value in (_mapping(receipt.get("failure")).get("code"), receipt.get("reason_code"), receipt.get("code"),
            receipt.get("error_code")):
        if isinstance(value, str) and value:
            return value
    return ""


def check_connection(connection, config, *, now) -> list[Violation]:
    """Every invariant this connection breaks, in priority order."""
    from workflow_runs.models import ContentFactoryRun
    from .system_stopped_runs import FOLLOWUP_KEY, stop_cause
    from .run_state import ARTICLE_WORKFLOWS
    from .website_connections import observed_source_sha
    found = []

    def add(invariant, subject, **details):
        found.append(Violation(connection.organization_id, str(connection.pk), invariant, str(subject), details))

    is_ready = ready(connection, config)
    blockers = _blocker_codes(connection)
    if is_ready and blockers & STALE_BLOCKERS:
        add("ready_with_scan_or_source_blocker", f"{connection.pk}-g{connection.generation}",
            blocker=sorted(blockers & STALE_BLOCKERS)[0])
    if connection.state == "connected" and "repository_source_changed" in blockers and _never_verified(connection):
        add("source_blocker_never_verified", f"{connection.pk}-g{connection.generation}")
    if connection.state == "connected":
        runs = ContentFactoryRun.objects.filter(organization_id=connection.organization_id,
            updated_at__gte=now - RUN_LOOKBACK).exclude(status="completed").order_by("-updated_at")
        for run in runs.only("pk", "run_id", "status", "result", "run_request")[:50]:
            saved = _mapping(run.run_request)
            if (saved.get("website_connection_id") == str(connection.pk)
                    and saved.get("connection_generation") == connection.generation
                    and _run_codes(run) & CONNECTION_REQUIRED_CODES):
                add("connection_required_failure_while_connected", run.run_id,
                    observed_code=sorted(_run_codes(run) & CONNECTION_REQUIRED_CODES)[0], run_status=run.status)
                break
    prepare = (connection.operations.filter(generation=connection.generation, action="prepare")
        .order_by("-created_at", "-pk").first())
    if prepare is not None and prepare.state == "needs_attention":
        code = _operation_code(prepare)
        if _registry_class(code) in PLATFORM_CLASSES:
            add("prepare_stopped_on_platform_code", prepare.pk, observed_code=code)
    if is_ready:
        from .website_setup_merge_proof import worker_publish_targets
        key = config.default_publish_target_id
        try:
            projected = [row for row in worker_publish_targets(config) or [] if _mapping(row).get("target_id") == key]
        except Exception:
            projected = []
        source = _mapping(_mapping(projected[0]).get("verification")).get("source_sha") if len(projected) == 1 else None
        if len(projected) != 1 or (isinstance(source, str) and source and source != connection.verified_sha):
            add("worker_projection_invalid_while_ready", f"{connection.pk}-{connection.verified_sha[:12]}",
                projected=len(projected))
    observed = observed_source_sha(connection)
    if connection.state == "connected" and connection.verified_sha and observed and observed != connection.verified_sha:
        snapshot = (connection.scan_snapshots.filter(generation=connection.generation, detector_version="github_head",
            source_sha=observed).order_by("created_at").first())
        open_job = connection.operations.filter(generation=connection.generation, action__in=JOB_ACTIONS,
            state__in=["pending", "running"]).exists()
        if snapshot is not None and snapshot.created_at <= now - HEAD_DRIFT_AFTER and not open_job:
            add("head_drift_without_job", f"{connection.pk}-{observed[:12]}", head=observed[:12])
    if is_ready:
        old = (connection.operations.filter(action__in=WORK_ACTIONS, state__in=OPEN_STATES,
            created_at__lte=now - OLD_OPERATION_AFTER).order_by("created_at").first())
        if old is not None:
            add("old_open_operation_on_ready", old.pk, action=old.action, state=old.state)
    if connection.state == "connected":
        stopped = (ContentFactoryRun.objects.filter(organization_id=connection.organization_id,
            workflow__in=ARTICLE_WORKFLOWS, status="cancelled", updated_at__gte=now - RUN_LOOKBACK,
            updated_at__lte=now - SYSTEM_STOP_GRACE).order_by("-updated_at"))
        for run in stopped[:50]:
            if FOLLOWUP_KEY not in _mapping(run.result) and stop_cause(run) is not None:
                add("system_stopped_run_without_notice", run.run_id)
                break
    return found


def find_violations(*, now=None, connection_ids=None) -> list[Violation]:
    """Each broken connection's violations, leading invariant first; reads local rows only."""
    from .models import OrganizationContentConfig
    now = now or timezone.now()
    rows = (OrganizationContentConfig.objects.select_related("website_connection__organization")
        .filter(website_connection__isnull=False, website_connection__state__in=["connected", "paused"]))
    if connection_ids is not None:
        rows = rows.filter(website_connection_id__in=list(connection_ids))
    found = []
    for config in rows.order_by("-website_connection__updated_at")[:CONNECTION_LIMIT]:
        found += check_connection(config.website_connection, config, now=now)
    return found


def alerts_for(violations) -> list[dict]:
    """One alert per connection: its leading violation, with the others listed."""
    by_connection = {}
    for item in violations:
        by_connection.setdefault(item.connection_id, []).append(item)
    alerts = []
    for connection_id, items in by_connection.items():
        items.sort(key=lambda item: INVARIANTS.index(item.invariant))
        lead = items[0]
        details = {"invariant": lead.invariant, "website_connection_id": connection_id, "subject": lead.subject,
            **{key: value for key, value in lead.details.items()}}
        if len(items) > 1:
            details["also"] = ":".join(item.invariant for item in items[1:])
        alerts.append({"organization_id": lead.organization_id, "slot": f"inv-{lead.invariant}-{lead.subject}"[:100],
            "details": details})
    return alerts


def run_connection_invariant_sweep(now=None) -> dict:
    """Queue one staff alert per broken connection and deliver them; one bounded pass an hour."""
    from django.core.cache import cache
    from organizations.models import Organization
    from . import founder_journey_alerts as alerts
    if not getattr(settings, "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", True):
        return {"status": "disabled"}
    now = now or timezone.now()
    if not cache.add(THROTTLE_KEY, now.isoformat(), SWEEP_INTERVAL_SECONDS):
        return {"status": "throttled"}
    pending = alerts_for(find_violations(now=now))
    organization_ids = {item["organization_id"] for item in pending}
    known = {pk: set(receipts) for pk, receipts in alerts._receipt_index(organization_ids)} if organization_ids else {}
    organizations = Organization.objects.only("pk").in_bulk(organization_ids)
    queued = 0
    for item in pending:
        if queued >= MAX_ALERTS_PER_SWEEP:
            break
        organization = organizations.get(item["organization_id"])
        if organization is None:
            continue
        alert_id = alerts.founder_journey_alert_id(organization_id=organization.pk, alert_reason=ALERT_REASON,
            slot=item["slot"])
        if alert_id in known.get(organization.pk, ()):
            continue
        alerts.queue_founder_journey_alert(organization=organization, alert_reason=ALERT_REASON,
            details=item["details"], slot=item["slot"])
        queued += 1
    delivered = alerts._deliver(now=now, limit=MAX_ALERTS_PER_SWEEP, organization_ids=organization_ids or None,
        reasons={ALERT_REASON}) if organization_ids else {"sent": 0, "unconfigured": 0, "undelivered": 0}
    return {"status": "completed", "broken_connections": len(pending), "queued": queued, **delivered}
