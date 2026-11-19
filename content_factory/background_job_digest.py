"""One digest rule for background jobs (plan step B6).

Nightly scheduler runs (``founder_journey_alerts.is_scheduler_run``) never alert
one by one: they are background work, not a founder's (A2). Org 175's nine
failed nights once became nine alerts. This digest is the only path that may
alert on them:

- ``background_job_failing``: the same startup's nightly job of one workflow
  failed on ``NIGHTS_RUNNING`` (3) consecutive Melbourne nights and is still
  failing. One alert per streak: the slot is keyed by the night the streak
  started, so a fourth or ninth failed night adds nothing. Two nights give none.
- ``background_job_failure_spike``: on one night at least ``SPIKE_SHARE`` (5%)
  of the startups that ran a nightly job had a failure, and at least
  ``SPIKE_MIN_ORGS`` of them (so one startup in a small fleet is not a spike).
  One global staff receipt per night, like the other global rate rules.

A night counts as failed for a startup and workflow when it has a failed
nightly run and no completed one. Reads are one bounded query over the last
``LOOKBACK_NIGHTS`` nights of scheduler rows, reading only small columns.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
import logging
import re
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

logger = logging.getLogger(__name__)
LOCAL = ZoneInfo("Australia/Melbourne")
NIGHTS_RUNNING = 3
LOOKBACK_NIGHTS = 8
SPIKE_SHARE = 0.05
SPIKE_MIN_ORGS = 3
FAILED_STATUSES = frozenset({"failed", "blocked", "precondition_failed", "preview_failed", "stopped", "denied"})
COMPLETED_STATUSES = frozenset({"completed"})
STREAK_REASON = "background_job_failing"
SPIKE_REASON = "background_job_failure_spike"


def _night(moment):
    return timezone.localtime(moment, LOCAL).date()


def _nights(now):
    """{(organization_id, workflow): {night: {"failed": bool, "completed": bool, "last_code": str}}}."""
    from .founder_journey_alerts import SAFE_CODE, founder_run_q
    start = datetime.combine(_night(now) - timedelta(days=LOOKBACK_NIGHTS), time.min, tzinfo=LOCAL)
    rows = (ContentFactoryRun.objects.exclude(founder_run_q()).filter(organization__isnull=False, created_at__gte=start)
            .order_by("created_at", "pk")
            .values_list("organization_id", "workflow", "status", "created_at", "result__error_code"))
    table = defaultdict(lambda: defaultdict(lambda: {"failed": False, "completed": False, "last_code": ""}))
    for organization_id, workflow, status, created_at, code in rows.iterator(chunk_size=500):
        night = table[(organization_id, str(workflow or ""))][_night(created_at)]
        if status in FAILED_STATUSES:
            night["failed"] = True
            if isinstance(code, str) and SAFE_CODE.fullmatch(code):
                night["last_code"] = code
        elif status in COMPLETED_STATUSES:
            night["completed"] = True
    return table


def failing_streak(nights) -> tuple | None:
    """(first night, length, latest code) of the failed streak ending at the latest decided night, else None.

    Nights still running (neither failed nor completed) are ignored; a completed
    night or a calendar gap ends a streak.
    """
    decided = sorted(night for night, state in nights.items() if state["failed"] or state["completed"])
    if not decided:
        return None
    latest = decided[-1]
    if nights[latest]["completed"] or not nights[latest]["failed"]:
        return None
    first, length = latest, 1
    while (previous := first - timedelta(days=1)) in nights and nights[previous]["failed"] and not nights[previous]["completed"]:
        first, length = previous, length + 1
    return first, length, nights[latest]["last_code"]


def _alerted_before(receipts, workflow, first) -> bool:
    """A streak alert for this workflow whose first night is on or before ``first``."""
    for receipt in receipts.values():
        details = receipt.get("details") if isinstance(receipt, dict) else None
        if (isinstance(details, dict) and receipt.get("alert_reason") == STREAK_REASON
                and details.get("workflow") == workflow and str(details.get("first_night") or "9999") <= first.isoformat()):
            return True
    return False


def _spike_nights(table):
    """{night: (failing orgs, orgs that ran a nightly job)}."""
    ran, failing = defaultdict(set), defaultdict(set)
    for (organization_id, _workflow), nights in table.items():
        for night, state in nights.items():
            ran[night].add(organization_id)
            # A failed night as the streak rule counts it: no completed run that night.
            if state["failed"] and not state["completed"]:
                failing[night].add(organization_id)
    return {night: (len(failing[night]), len(orgs)) for night, orgs in ran.items()}


def _queue_spike(night, failing, total, *, now) -> int:
    from .founder_journey_alerts import ack_due_at
    from .staff_alert_rules import GLOBAL_TTL, global_key
    midnight = datetime.combine(night, time.min, tzinfo=LOCAL)
    key = global_key(SPIKE_REASON, int(midnight.timestamp()) // 86400)
    receipt = {"alert_id": key, "alert_reason": SPIKE_REASON, "queued_at": now.isoformat(), "state": "unconfigured",
               "run_id": None, "details": {"ack_due_at": ack_due_at(now).isoformat(), "night": night.isoformat(),
                                           "failing_orgs": failing, "orgs": total}}
    added = cache.add(key, receipt, GLOBAL_TTL)
    if added:
        logger.warning("staff_alert rule=%s night=%s failing_orgs=%s orgs=%s", SPIKE_REASON, night, failing, total)
    return int(added)


def run_background_job_digest_sweep(now=None, limit=None) -> dict:
    """The self-heal sweep entry point; off with the staff alert rules (``ARTICLES_STAFF_ALERT_RULES_ENABLED``)."""
    from django.conf import settings
    if not getattr(settings, "ARTICLES_STAFF_ALERT_RULES_ENABLED", False):
        return {"status": "disabled"}
    return {"status": "completed", **run_background_job_digest(now=now)}


def run_background_job_digest(now=None) -> dict:
    """Queue the digest's alerts; safe to run every tick (every alert has a stable id)."""
    from organizations.models import Organization
    from .founder_journey_alerts import _receipt_index, founder_journey_alert_id, queue_founder_journey_alert
    now = now or timezone.now()
    table = _nights(now)
    streaks = []
    for (organization_id, workflow), nights in table.items():
        streak = failing_streak(nights)
        if streak is not None and streak[1] >= NIGHTS_RUNNING:
            streaks.append((organization_id, workflow, *streak))
    queued = 0
    if streaks:
        known = dict(_receipt_index({row[0] for row in streaks}))
        organizations = Organization.objects.in_bulk({row[0] for row in streaks})
        window_start = _night(now) - timedelta(days=LOOKBACK_NIGHTS)
        for organization_id, workflow, first, length, code in sorted(streaks, key=lambda row: (row[0], row[1])):
            slot = f"nightly-{re.sub(r'[^A-Za-z0-9_.-]', '_', workflow)[:40]}-{first.isoformat()}"
            key = founder_journey_alert_id(organization_id=organization_id, alert_reason=STREAK_REASON, slot=slot)
            organization = organizations.get(organization_id)
            receipts = known.get(organization_id, {})
            if organization is None or key in receipts:
                continue
            if first <= window_start and _alerted_before(receipts, workflow, first):
                # The streak is older than the lookback, so its first night is cut
                # off here; it already alerted under its real first night.
                continue
            details = {"workflow": workflow, "nights": length, "first_night": first.isoformat()}
            if code:
                details["last_code"] = code
            queue_founder_journey_alert(organization=organization, alert_reason=STREAK_REASON, slot=slot, details=details)
            queued += 1
    spikes = 0
    for night, (failing, total) in sorted(_spike_nights(table).items())[-2:]:
        if total and failing >= SPIKE_MIN_ORGS and failing / total >= SPIKE_SHARE:
            spikes += _queue_spike(night, failing, total, now=now)
    return {"streaks": len(streaks), "queued": queued, "spikes": spikes}
