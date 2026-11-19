"""'We're fixing it' for website setup, with a bounded life (plan B11, B25).

When Content Factory's repair loop runs out on a setup candidate (its repair
receipt says so, whatever the stop code), the founder has nothing to do: the
fault is ours. The preparation is marked ``staff_fixing``, staff are alerted
with the validator output and the last candidate's run, and the founder is
told nothing is needed.

That state has a bounded life, so it never becomes a silent stall:

- a named staff owner within one working day (Melbourne), else a staff alert;
- a founder update at 72 hours;
- by day 7 it ends as Ready, a named founder ask, or a closed-list
  "can't be done yet" with the goal's refund (D7) and a lead row.

The deadline is fixed when the state is first entered and is never extended:
re-entering after a push or retry keeps the original start.

The record lives in the operation's JSON receipt; no schema change.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

KEY = "staff_fixing"
STATUS = "staff_fixing"
LIFETIME = timedelta(days=7)
FOUNDER_UPDATE_AFTER = timedelta(hours=72)
WORKDAY_TIMEZONE = ZoneInfo("Australia/Melbourne")
EXITS = frozenset({"ready", "named_ask", "cant_be_done_yet"})
# The closed list of "can't be done yet" causes (plan 1.1, B25). A staff fix
# that does not land by day 7 is recorded honestly as ``not_fixed_in_time``.
CANT_BE_DONE_REASONS = frozenset({
    "cms_content", "no_code_export", "not_on_github", "server_rendered_stack", "not_fixed_in_time",
})
LEAD_REASON = "setup_cant_be_done_yet"
REPAIR_STOPS = frozenset({"repair_cap_reached", "no_new_candidate", "findings_unchanged"})


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else None


def next_working_day(moment):
    """The same local time on the next Melbourne working day (weekends skipped)."""
    local = moment.astimezone(WORKDAY_TIMEZONE) + timedelta(days=1)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local.astimezone(moment.tzinfo or timezone.utc)


def repair_exhausted(result) -> dict | None:
    """Content Factory's repair receipt when its repair loop ran out, else None.

    Keyed on the receipt, never on the code ``DIRECTORY_REPAIR_EXHAUSTED``:
    a flag-off run reports its original gate code with the same receipt.
    """
    if not isinstance(result, dict):
        return None
    for source in (result, result.get("article_system_setup")):
        receipt = source.get("repair_receipt") if isinstance(source, dict) else None
        if (isinstance(receipt, dict) and receipt.get("exhausted") is True
                and receipt.get("stop") in REPAIR_STOPS):
            return receipt
    return None


def record(op) -> dict | None:
    value = (op.receipt or {}).get(KEY)
    return value if isinstance(value, dict) and _stamp(value.get("since")) else None


def active(op) -> bool:
    value = record(op)
    return value is not None and value.get("exit") is None


def message():
    """Founder copy: ours to fix, nothing needed, never a staff claim no alert would back."""
    from .hosted_quality_state import staff_fixing_message
    return staff_fixing_message()


def enter(op, *, now, code, step, repair_receipt, run_id=""):
    """Mark ``op`` staff_fixing and alert staff once. The caller persists ``op``."""
    previous = record(op)
    since = _stamp(previous.get("since")) if previous else None
    if since is None or previous.get("exit") is not None:
        since = now
    receipt = dict(op.receipt or {})
    escalations = dict(previous.get("escalations") or {}) if previous and previous.get("exit") is None else {}
    finding_codes = [str(item)[:80] for item in (repair_receipt or {}).get("finding_codes") or [] if isinstance(item, str)][:20]
    receipt[KEY] = {
        "version": 1, "since": since.isoformat(), "deadline_at": (since + LIFETIME).isoformat(),
        "owner_due_at": next_working_day(since).isoformat(),
        "founder_update_due_at": (since + FOUNDER_UPDATE_AFTER).isoformat(),
        "reason": "repair_exhausted", "stop_code": str(code or "")[:80], "step": str(step or "")[:80],
        "run_id": str(run_id or receipt.get("child_run_id") or "")[:120],
        "repair_stop": str((repair_receipt or {}).get("stop") or "")[:40],
        "repair_rounds": int((repair_receipt or {}).get("rounds") or 0),
        "finding_codes": finding_codes,
        "owner": (previous or {}).get("owner") if previous and previous.get("exit") is None else None,
        "escalations": escalations, "exit": None,
    }
    receipt.pop("user_action", None)
    receipt.update(status=STATUS, message=message(), retryable=False, hold_owner="staff")
    op.receipt = receipt
    op.state, op.next_attempt_at = "needs_attention", None
    if "entered" not in escalations:
        _alert(op, "repair_loop_exhausted", "entered", now=now, details={
            "stop_code": receipt[KEY]["stop_code"] or "unknown", "step": receipt[KEY]["step"] or "setup",
            "repair_stop": receipt[KEY]["repair_stop"] or "unknown", "repair_rounds": receipt[KEY]["repair_rounds"],
            "finding_count": len(finding_codes), "first_finding_code": finding_codes[0] if finding_codes else "none"})
    return op


def assign_owner(op, *, staff_user, now=None):
    """Name the staff owner. Only an active staff account can own the fix."""
    if not (getattr(staff_user, "is_active", False) and getattr(staff_user, "is_staff", False)):
        raise PermissionError("Only an active staff account can own a staff fix.")
    value = record(op)
    if value is None or value.get("exit") is not None:
        raise ValueError("This preparation is not being fixed by staff.")
    now = now or timezone.now()
    receipt = dict(op.receipt)
    receipt[KEY] = {**value, "owner": {"user_id": staff_user.pk, "at": now.isoformat()}}
    op.receipt = receipt
    return op


def close(op, exit, *, now=None, reason=None, user_action=None):
    """End staff_fixing as Ready, a named founder ask, or "can't be done yet".

    The caller holds the operation's lock and persists it. "Can't be done yet"
    stops the preparation, reserves the goal's refund (D7) and records a lead
    row; it never re-opens the preparation by itself.
    """
    if exit not in EXITS:
        raise ValueError("Unknown staff-fixing exit")
    value = record(op)
    if value is None or value.get("exit") is not None:
        return op
    now = now or timezone.now()
    receipt = dict(op.receipt)
    closed = {**value, "exit": exit, "closed_at": now.isoformat()}
    if exit == "named_ask":
        from .founder_journey import FOUNDER_ASKS
        if not isinstance(user_action, dict) or user_action.get("id") not in FOUNDER_ASKS:
            raise ValueError("A named ask needs one of the founder asks")
        op.state, op.next_attempt_at = "needs_user", None
        receipt.update(status="needs_you", user_action=dict(user_action), failure_class="needs_user", retryable=False)
        receipt.pop("hold_owner", None)
    elif exit == "cant_be_done_yet":
        if reason not in CANT_BE_DONE_REASONS:
            raise ValueError("Can't-be-done-yet needs a reason from the closed list")
        closed["cant_be_done_reason"] = reason
        op.state, op.next_attempt_at = "stopped", None
        receipt.pop("user_action", None)
        receipt.update(status="cant_be_done_yet", retryable=False,
            message="We couldn't make your website work with MLAI articles yet. Anything you paid for this "
                    "setup is refunded, and we'll tell you when that changes.")
        receipt.pop("hold_owner", None)
    else:
        receipt.pop("hold_owner", None)
    receipt[KEY] = closed
    op.receipt = receipt
    if exit == "cant_be_done_yet":
        _refund_and_record_lead(op, reason=reason, now=now)
    return op


def _refund_and_record_lead(op, *, reason, now):
    from .website_first_article_refunds import reserve_refund
    try:
        reserve_refund(op, now=now)
    except Exception:  # noqa: BLE001 - the stop stands; staff see the failed refund
        logger.error("staff_fixing_refund_reservation_failed operation=%s", op.pk, exc_info=True)
        _alert(op, "our_team_item", "refund_failed", now=now, details={"reason_detail": "refund_reservation_failed"})
    try:
        from founder_tools.website_leads import record_staff_lead
        record_staff_lead(op.connection.organization, reason=LEAD_REASON, cause=reason,
                          domain=getattr(op.connection.organization, "domain", "") or "", now=now)
    except Exception:  # noqa: BLE001 - a lead row never blocks the founder's answer
        logger.error("staff_fixing_lead_row_failed operation=%s", op.pk, exc_info=True)


def _alert(op, reason, escalation, *, now, details=None):
    """Queue one staff alert per staff-fixing occurrence and bound."""
    from .founder_journey_alerts import queue_founder_journey_alert
    value = record(op) or {}
    since = _stamp(value.get("since")) or now
    slot = f"staff-fixing-{op.pk}-{int(since.timestamp())}-{escalation}"
    try:
        queue_founder_journey_alert(organization=op.connection.organization, alert_reason=reason, slot=slot,
            company_id=(op.payload or {}).get("company_id"),
            details={"kind": "staff_fixing", "website_operation_id": str(op.pk), "escalation": escalation,
                     "run_id": value.get("run_id") or "none", "deadline_at": value.get("deadline_at"),
                     **(details or {})})
    except Exception:  # noqa: BLE001 - an alert failure must not stop the state change
        logger.error("staff_fixing_alert_failed operation=%s escalation=%s", op.pk, escalation, exc_info=True)
        return False
    if op.receipt.get(KEY):
        escalations = dict(op.receipt[KEY].get("escalations") or {})
        escalations[escalation] = now.isoformat()
        op.receipt = {**op.receipt, KEY: {**op.receipt[KEY], "escalations": escalations}}
    return True


def escalate(op, *, now):
    """Apply every bound that has passed; returns the escalations applied."""
    value = record(op)
    if value is None or value.get("exit") is not None:
        return []
    applied = []
    since = _stamp(value["since"])
    deadline = since + LIFETIME
    escalations = value.get("escalations") or {}
    if now >= next_working_day(since) and not value.get("owner") and "owner_due" not in escalations:
        if _alert(op, "our_team_item", "owner_due", now=now, details={"reason_detail": "no_named_owner"}):
            applied.append("owner_due")
    if now >= since + FOUNDER_UPDATE_AFTER and "founder_update" not in (record(op).get("escalations") or {}):
        from .founder_transitions import notify_staff_fixing
        notify_staff_fixing(op.connection.organization, op, "staff_fixing_update")
        receipt = dict(op.receipt)
        receipt[KEY] = {**receipt[KEY], "escalations": {**receipt[KEY]["escalations"], "founder_update": now.isoformat()}}
        op.receipt = receipt
        applied.append("founder_update")
    if now >= deadline:
        close(op, "cant_be_done_yet", now=now, reason="not_fixed_in_time")
        _alert(op, "our_team_item", "day_7_exit", now=now, details={"reason_detail": "not_fixed_in_time"})
        from .founder_transitions import notify_staff_fixing
        notify_staff_fixing(op.connection.organization, op, "staff_fixing_cant_be_done")
        applied.append("day_7_exit")
    return applied


def run_staff_fixing_sweep(now=None, limit=None) -> dict:
    """Escalate every open staff_fixing preparation at its bounds (B25). No flag: it bounds a state."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    limit = limit or 200
    candidates = list(WebsiteConnectionOperation.objects.filter(action="prepare", state="needs_attention",
        receipt__status=STATUS).order_by("updated_at").values_list("pk", flat=True)[:limit])
    counts = {"checked": 0, "owner_due": 0, "founder_update": 0, "day_7_exit": 0}
    counts["failed"] = 0
    for pk in candidates:
        try:
            with transaction.atomic():
                op = WebsiteConnectionOperation.objects.select_for_update().select_related(
                    "connection", "connection__organization").filter(pk=pk).first()
                if op is None or not active(op):
                    continue
                counts["checked"] += 1
                applied = escalate(op, now=now)
                if applied:
                    WebsiteConnectionOperation.objects.filter(pk=op.pk).update(
                        state=op.state, receipt=op.receipt, next_attempt_at=op.next_attempt_at, updated_at=now)
        except Exception:  # noqa: BLE001 - one bad row must not hold every other fix past its bounds
            # The row's changes roll back; the next sweep applies its bounds again.
            counts["failed"] += 1
            logger.error("staff_fixing_sweep_row_failed operation=%s", pk, exc_info=True)
            continue
        for name in applied:
            counts[name] += 1
    return counts


def report(now=None) -> dict:
    """Count and age of open staff_fixing items, by stack, for the daily report (F9)."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    rows = WebsiteConnectionOperation.objects.filter(action="prepare", receipt__status=STATUS).select_related("connection")
    stacks = {}
    for op in rows:
        value = record(op)
        if value is None or value.get("exit") is not None:
            continue
        capabilities = op.connection.capabilities if isinstance(op.connection.capabilities, dict) else {}
        stack = str(capabilities.get("framework") or capabilities.get("stack") or "unknown")[:40]
        age = (now - _stamp(value["since"])).total_seconds() / 3600
        entry = stacks.setdefault(stack, {"count": 0, "oldest_hours": 0.0})
        entry["count"] += 1
        entry["oldest_hours"] = max(entry["oldest_hours"], round(age, 1))
    return {"stacks": stacks, "total": sum(item["count"] for item in stacks.values())}
