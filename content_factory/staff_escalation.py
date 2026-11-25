"""The escalation ladder as data on each staff console item (plan 7.6, F10).

Nothing here sends. Each rung names the existing sender that acts on it:

- a generic or internal stop: a staff alert at once (``no_generic_stop`` and
  the stuck sweep's stopped-operation alert);
- a failed run: a staff alert after :data:`FAILED_RUN_AFTER` (the stuck sweep,
  ``founder_stuck_alerts.run_stuck_after()``; six hours until
  ``ARTICLES_STAFF_ESCALATION_LADDER_ENABLED`` is on);
- an unanswered ask: founder reminders at 24 and 72 hours and one staff alert
  at the ask's threshold, all from E7's ladder (``founder_transitions``); no
  second sender;
- anything still waiting on us after :data:`OWNER_AFTER`: one owner escalation
  through the stuck sweep (same channel, same kill switch), sent only with
  ``ARTICLES_STAFF_ESCALATION_LADDER_ENABLED`` (shadow until then);
- ``staff_fixing`` (B25): a named staff owner within one working day, a
  founder update at 72 hours, and an end by day 7 (``website_staff_fixing``).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

GENERIC_OR_INTERNAL_AFTER = timedelta(0)
FAILED_RUN_AFTER = timedelta(hours=2)
OWNER_AFTER = timedelta(hours=48)
# The owner rung is only sent for a stop that crossed 48 hours within this
# horizon, so switching it on does not page the owner for months-old history.
OWNER_HORIZON = timedelta(days=7)
INTERNAL_KINDS = frozenset({"operation_retired_ask", "operation_budget"})


def _moment(value):
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        return moment if timezone.is_aware(moment) else None
    return None


def _rung(rung_id, due, *, now, sender, done=None):
    reached = now >= due
    return {"id": rung_id, "dueAt": due.isoformat(), "reached": reached,
            "done": bool(done) if done is not None else reached, "sender": sender}


def generic_or_internal(condition) -> bool:
    """A stop that is ours with no founder story: generic, unregistered, ask-less, or an internal hold."""
    if condition.kind in INTERNAL_KINDS:
        return True
    from .no_generic_stop import generic_stop_reason
    return generic_stop_reason(condition.code or None) is not None


def condition_kind(condition) -> str:
    if condition.details.get("staff_fixing_since"):
        return "staff_fixing"
    if generic_or_internal(condition):
        return "generic_or_internal_stop"
    if condition.kind == "run_stopped":
        return "failed_run"
    return "stopped_step"


def _staff_fixing_rungs(condition, *, now):
    from .website_staff_fixing import FOUNDER_UPDATE_AFTER, LIFETIME, next_working_day
    since = _moment(condition.details.get("staff_fixing_since")) or condition.stuck_since
    return since, [
        _rung("staff_owner_named", next_working_day(since), now=now, sender="staff_fixing_sweep",
              done=bool(condition.details.get("staff_fixing_owner"))),
        _rung("founder_update", since + FOUNDER_UPDATE_AFTER, now=now, sender="staff_fixing_sweep"),
        _rung("day_7_exit", since + LIFETIME, now=now, sender="staff_fixing_sweep"),
    ]


def _stop_rungs(condition, kind, *, now):
    from .founder_stuck_alerts import escalation_ladder_enabled, run_stuck_after
    since = condition.stuck_since
    # The wait the stuck sweep really uses (two hours once the ladder is on, six in shadow).
    first = run_stuck_after() if kind == "failed_run" else GENERIC_OR_INTERNAL_AFTER
    sender = "generic_stop_alert" if kind == "generic_or_internal_stop" and condition.kind == "run_stopped" else "founder_stuck"
    return since, [_rung("staff_alert", since + first, now=now, sender=sender),
                   _rung("owner", since + OWNER_AFTER, now=now,
                         sender="founder_stuck" if escalation_ladder_enabled() else "shadow_not_sent")]


def _ask_rungs(ask, since, *, now):
    from .founder_transitions import REMINDER_HOURS, staff_threshold
    rungs = [_rung(f"founder_reminder_{hours}h", since + timedelta(hours=hours), now=now, sender="ask_ladder")
             for hours in REMINDER_HOURS]
    rungs.append(_rung("staff_alert", since + timedelta(hours=staff_threshold(ask)), now=now, sender="ask_ladder"))
    return sorted(rungs, key=lambda rung: rung["dueAt"])


def _shape(kind, since, rungs, *, now, **extra):
    reached = [rung for rung in rungs if rung["reached"]]
    upcoming = [rung for rung in rungs if not rung["reached"]]
    return {"kind": kind, "since": since.isoformat(), "ageMinutes": max(0, int((now - since).total_seconds() // 60)),
            "rung": reached[-1]["id"] if reached else None,
            "next": {"id": upcoming[0]["id"], "dueAt": upcoming[0]["dueAt"]} if upcoming else None,
            "rungs": rungs, **extra}


def ladder(*, now, stuck=(), ask=None, ask_since=None) -> dict | None:
    """The startup's escalation state: its oldest wait on us, else its founder ask, else None."""
    if stuck:
        condition = min(stuck, key=lambda item: item.stuck_since)
        kind = condition_kind(condition)
        since, rungs = (_staff_fixing_rungs(condition, now=now) if kind == "staff_fixing"
                        else _stop_rungs(condition, kind, now=now))
        return _shape(kind, since, rungs, now=now, waitingOn="mlai", stopCode=condition.code or None,
                      step=condition.step, conditions=len(stuck))
    since = _moment(ask_since)
    if ask and since is not None:
        return _shape("unanswered_ask", since, _ask_rungs(ask, since, now=now), now=now, waitingOn="founder", ask=ask)
    return None


def ask_since(config, ask) -> datetime | None:
    """When the founder's current ``ask`` began, from E7's stored notices (no query)."""
    if not ask:
        return None
    strategy = getattr(config, "pillar_strategy", None)
    notices = strategy.get("founder_notices") if isinstance(strategy, dict) else None
    last = notices.get("last") if isinstance(notices, dict) else None
    moments = [_moment(entry.get("at")) for entry in (last or {}).values()
               if isinstance(entry, dict) and entry.get("state") == "your_turn" and entry.get("ask") == ask]
    moments = [moment for moment in moments if moment is not None]
    return min(moments) if moments else None


def owner_escalations(conditions, *, now):
    """The stuck conditions due their one owner escalation now (48 hours, within the horizon)."""
    from dataclasses import replace
    due = []
    for condition in conditions:
        if condition.details.get("staff_fixing_since"):
            continue  # B25 owns staff_fixing's bounds.
        age = now - condition.stuck_since
        if OWNER_AFTER <= age < OWNER_AFTER + OWNER_HORIZON:
            due.append(replace(condition, slot=f"{condition.slot}-owner",
                               details={**condition.details, "escalation": "owner"}))
    return due
