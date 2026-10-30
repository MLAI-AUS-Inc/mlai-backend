"""Content Factory's append-only run events, kept as a bounded log on the run.

Snapshots stay the compatibility record. Events are small facts with a per-run
monotonic ``seq``: a step started or finished, a failure (code and class), an
artifact, or a step's model cost. Behind ARTICLES_RUN_EVENTS_ENABLED they are
accepted idempotently by (run_id, seq), folded into per-step summaries and
kept in the run's existing ``result`` JSON, so no table or migration is needed.

Events never change a run's status, billing, approval or repository state.
They only let readers see step progress when snapshots lag.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

EVENTS_RESULT_KEY = "run_events_v1"
EVENT_TYPES = frozenset({"step_started", "step_done", "failure", "artifact", "cost"})
DONE_STATUSES = frozenset({"completed", "skipped", "failed", "blocked"})
MAX_BATCH = 100
MAX_SEQ = 2 ** 53
RECENT_LIMIT = 100
STEP_LIMIT = 80
FAILURE_LIMIT = 20
INTERVAL_LIMIT = 64
CLOSED_RUN_STATUSES = frozenset({"cancelled", "denied"})
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_RANK = {"pending": 0, "running": 1, "completed": 2, "skipped": 2, "failed": 2, "blocked": 2}
_COST_COUNTS = ("calls", "tokens", "input_tokens", "output_tokens")


def run_events_enabled():
    return getattr(settings, "ARTICLES_RUN_EVENTS_ENABLED", False) is True


def is_run_events(payload):
    return (run_events_enabled() and isinstance(payload, dict)
            and (payload.get("event") or payload.get("event_type")) == "run_events")


def _count(value):
    return value if type(value) is int and 0 <= value < MAX_SEQ else None


def _token(value):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat() if parsed.tzinfo else None


def _usd(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _registered_failure(raw):
    """Project a reported failure onto the registry; the class comes from it too.

    A well-formed code this backend's registry does not know yet (Content
    Factory can ship codes first) keeps the failure without a code.
    """
    from .website_failure_contract import CLASSES, load_registry
    entry = load_registry()["by_code"].get(raw.get("code"))
    if entry is None:
        reported = raw.get("failure_class")
        return {"unregistered_code": True, **({"failure_class": reported} if reported in CLASSES else {})}
    return {"code": entry["code"], "failure_class": entry["failure_class"]}


def clean_event(raw):
    """Keep only typed facts; worker text and paths never reach the log."""
    if not isinstance(raw, dict):
        return None
    seq, kind = raw.get("seq"), raw.get("type")
    if type(seq) is not int or not 0 < seq < MAX_SEQ or kind not in EVENT_TYPES:
        return None
    event = {"seq": seq, "type": kind}
    step = _token(raw.get("step"))
    if raw.get("step") is not None and step is None:
        return None
    if step:
        event["step"] = step
    elif kind in {"step_started", "step_done", "artifact"}:
        return None
    for key in ("attempt", "generation"):
        if _count(raw.get(key)) is not None:
            event[key] = raw[key]
    at = _timestamp(raw.get("at"))
    if at:
        event["at"] = at
    if kind == "step_done":
        status = raw.get("status") or "completed"
        if status not in DONE_STATUSES:
            return None
        event["status"] = status
    elif kind == "failure":
        if _token(raw.get("code")) is None:
            return None
        event.update(_registered_failure(raw))
        if type(raw.get("retryable")) is bool:
            event["retryable"] = raw["retryable"]
    elif kind == "artifact":
        if _token(raw.get("kind")):
            event["kind"] = raw["kind"]
    elif kind == "cost":
        for key in _COST_COUNTS:
            if _count(raw.get(key)) is not None:
                event[key] = raw[key]
        usd = _usd(raw.get("usd"))
        if usd is not None:
            event["usd"] = str(usd)
        if _token(raw.get("model")):
            event["model"] = raw["model"]
    return event


def _covered(intervals, seq):
    return any(start <= seq <= end for start, end in intervals)


def _add_interval(intervals, seq):
    merged = sorted([*intervals, [seq, seq]])
    compact = []
    for start, end in merged:
        if compact and start <= compact[-1][1] + 1:
            compact[-1][1] = max(compact[-1][1], end)
        else:
            compact.append([start, end])
    while len(compact) > INTERVAL_LIMIT:
        # Close the oldest gap; a very late event inside it is then a duplicate.
        compact[0:2] = [[compact[0][0], compact[1][1]]]
    return compact


def _empty_log():
    return {"schema_version": 1, "applied": [], "last_seq": 0, "accepted": 0, "duplicates": 0,
            "rejected": 0, "dropped_steps": 0, "recent": [], "steps": {}, "current_step": None,
            "failures": [], "cost": {"by_step": {}, "totals": {}}, "updated_at": None}


def _valid_log(log):
    if not isinstance(log, dict) or log.get("schema_version") != 1:
        return _empty_log()
    merged = {**_empty_log(), **log}
    for key, kind in (("applied", list), ("recent", list), ("steps", dict), ("failures", list), ("cost", dict)):
        if not isinstance(merged.get(key), kind):
            merged[key] = _empty_log()[key]
    return merged


def _step(log, name):
    steps = log["steps"]
    if name not in steps and len(steps) >= STEP_LIMIT:
        log["dropped_steps"] += 1
        return None
    return steps.setdefault(name, {"status": "pending", "seq": 0, "artifacts": 0})


def _apply(log, event):
    seq, kind, name = event["seq"], event["type"], event.get("step")
    state = _step(log, name) if name else None
    if name and state is None:
        return
    if kind in {"step_started", "step_done"} and seq > state.get("seq", 0):
        state.update(status="running" if kind == "step_started" else event["status"], seq=seq)
        if "attempt" in event:
            state["attempt"] = event["attempt"]
        if "at" in event:
            state["started_at" if kind == "step_started" else "completed_at"] = event["at"]
        if kind == "step_started":
            state.pop("completed_at", None)
            current = log.get("current_step")
            if not isinstance(current, dict) or seq > current.get("seq", 0):
                log["current_step"] = {"step": name, "seq": seq}
    elif kind == "failure":
        failure = {key: event[key] for key in ("seq", "step", "code", "unregistered_code", "failure_class", "retryable", "at")
                   if key in event}
        log["failures"] = sorted([*log["failures"], failure], key=lambda item: item.get("seq", 0))[-FAILURE_LIMIT:]
        if state is not None and seq > state.get("failure", {}).get("seq", 0):
            state["failure"] = {key: failure[key] for key in ("seq", "code", "failure_class") if key in failure}
    elif kind == "artifact":
        state["artifacts"] = state.get("artifacts", 0) + 1
    elif kind == "cost":
        # Content Factory reports each step's running total, so keep the newest.
        if not isinstance(log["cost"].get("by_step"), dict):
            log["cost"]["by_step"] = {}
        by_step = log["cost"]["by_step"]
        key = name or "_run"
        if seq > by_step.get(key, {}).get("seq", 0) and (key in by_step or len(by_step) < STEP_LIMIT):
            by_step[key] = {field: event[field] for field in ("seq", *_COST_COUNTS, "usd", "model") if field in event}
        totals = {field: sum(row.get(field, 0) for row in by_step.values()) for field in _COST_COUNTS}
        totals["usd"] = str(sum((Decimal(row.get("usd", "0")) for row in by_step.values()), Decimal(0)))
        log["cost"]["totals"] = totals


def fold_events(log, events, *, now=None):
    """Apply new events once each; return (log, accepted, duplicates)."""
    log = _valid_log(log)
    accepted = duplicates = 0
    for event in sorted(events, key=lambda item: item["seq"]):
        if _covered(log["applied"], event["seq"]):
            duplicates += 1
            continue
        log["applied"] = _add_interval(log["applied"], event["seq"])
        log["last_seq"] = max(log["last_seq"], event["seq"])
        _apply(log, event)
        log["recent"] = sorted([*log["recent"], event], key=lambda item: item["seq"])[-RECENT_LIMIT:]
        accepted += 1
    log["accepted"] += accepted
    log["duplicates"] += duplicates
    if accepted:
        log["updated_at"] = (now or timezone.now()).isoformat()
    return log, accepted, duplicates


def _binding_matches(run, payload):
    """Events carry the run's own connection identity, never a new one."""
    from .snapshot_meaning import same_meaning
    from .website_contract import WebsiteAuthorityError, connection_contract
    domain = payload.get("domain")
    if domain and not same_meaning(run.domain, domain, "domain"):
        return False
    try:
        reported = connection_contract(payload)
        saved = connection_contract(run.run_request or {})
    except WebsiteAuthorityError:
        return False
    return all(same_meaning(saved.get(key), value, key) for key, value in reported.items())


def receive_run_events(payload):
    """Accept a batch of events for one run. Duplicates are acknowledged."""
    from django.db import transaction
    from rest_framework.response import Response
    from workflow_runs.models import ContentFactoryRun
    run_id = str(payload.get("run_id") or payload.get("job_id") or "")
    raw = payload.get("events")
    if not 0 < len(run_id) <= 100 or not isinstance(raw, list) or not 0 < len(raw) <= MAX_BATCH:
        return Response({"error": "run_events_invalid", "retryable": False}, status=400)
    events = [event for event in map(clean_event, raw) if event]
    rejected = len(raw) - len(events)
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id).first()
        if run is None:
            # Events can precede the first snapshot; the sender retries later.
            return Response({"status": "pending", "reason": "run_not_found", "run_id": run_id,
                             "retryable": True}, status=409)
        if run.status in CLOSED_RUN_STATUSES:
            return Response({"status": "ignored", "reason": "run_closed", "run_id": run_id}, status=200)
        if not _binding_matches(run, payload):
            # Permanent for this batch. No ``code``: the run itself is not refused.
            return Response({"error": "run_events_scope_mismatch", "run_id": run_id, "retryable": False}, status=409)
        result = dict(run.result or {}) if isinstance(run.result, dict) else {}
        log, accepted, duplicates = fold_events(result.get(EVENTS_RESULT_KEY), events)
        log["rejected"] += rejected
        if accepted or rejected:
            result[EVENTS_RESULT_KEY] = log
            run.result = result
            # Not updated_at: stall detection keeps reading the snapshot clock.
            run.save(update_fields=["result"])
    if rejected:
        logger.warning("content_factory_run_events_rejected run_id=%s rejected=%s", run_id[:100], rejected)
    return Response({"status": "received", "run_id": run_id, "accepted": accepted, "duplicates": duplicates,
                     "rejected": rejected, "last_seq": log["last_seq"]}, status=200)


def event_progress(run, steps):
    """Steps whose events are ahead of the last snapshot, for readers to show.

    ``steps`` is the serialised snapshot step map. A step is ahead when its
    (attempt, phase) from events is later than the snapshot row's.
    """
    log = (run.result or {}).get(EVENTS_RESULT_KEY) if isinstance(run.result, dict) else None
    if not isinstance(log, dict) or log.get("schema_version") != 1:
        return None
    log = _valid_log(log)
    ahead = {}
    for name, state in log["steps"].items():
        if not isinstance(state, dict):
            continue
        snapshot = steps.get(name) or {}
        snapshot_key = (_count(snapshot.get("attempts")) or 0, _RANK.get(snapshot.get("status"), 0))
        event_key = (_count(state.get("attempt")) or 0, _RANK.get(state.get("status"), 0))
        if event_key > snapshot_key:
            ahead[name] = {key: state[key] for key in ("status", "attempt", "seq", "started_at", "completed_at", "failure")
                           if key in state}
    current = log.get("current_step") if isinstance(log.get("current_step"), dict) else {}
    current_step = current.get("step") if current.get("step") in ahead else run.current_step
    return {"last_seq": log["last_seq"], "snapshot_lagging": bool(ahead), "current_step": current_step,
            "steps_ahead": ahead, "cost": log["cost"].get("totals") or {}, "failures": log["failures"][-5:]}
