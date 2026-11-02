"""Typed, admitted run events; legacy observation and snapshot paths stay separate.

The backend's admission and execution ticket are authority. Worker events are
immutable observations within that exact ticket, never permission, approval,
billing or delivery proof. This module performs no remote effects.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re

from .run_status_contract import RUN_STATUSES, TERMINAL


EVENT_NAME = "run_events_v2"
SCHEMA_VERSION = 1
MAX_BATCH = 100
MAX_NUMBER = 2 ** 53
MAX_EVENT_BYTES = 65_536
MAX_ATTEMPT = 2_147_483_647
WIRE_ADMISSION = "backend_run_admission_v1"
WIRE_EXECUTION = "backend_run_execution_v1"
EVENT_TYPES = frozenset({"run_state", "step_started", "step_done", "failure", "artifact", "cost"})
STEP_DONE = frozenset({"completed", "skipped", "failed", "blocked", "cancelled"})
HARD_TERMINAL = frozenset({"completed", "cancelled", "denied"})
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,119}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_SCOPE = frozenset({"website_connection_id", "connection_generation", "connection_target_id", "repository_id",
                    "operation_id", "operation_attempt", "deletion_epoch", "expected_source_sha", "source_run_id"})
_BASE = frozenset({"seq", "generation", "state_version", "type", "at", "digest"})
_COUNTS = frozenset({"calls", "tokens", "input_tokens", "output_tokens"})
_FIELDS = {
    "run_state": ({"status"}, {"step"}),
    "step_started": ({"step", "attempt"}, set()),
    "step_done": ({"step", "attempt", "status"}, set()),
    "failure": ({"failure"}, {"step"}),
    "artifact": ({"step", "kind"}, {"artifact_id", "content_sha256"}),
    "cost": (set(), {*_COUNTS, "usd", "step", "model"}),
}


class RunEventInvalid(ValueError):
    """An event has an invalid bounded shape or content digest."""


class RunEventConflict(ValueError):
    """A retained event identity or ledger cannot be replaced."""


@dataclass(frozen=True)
class ValidatedBatch:
    """An isolated copy of a syntactically valid, digest-checked batch."""

    run_id: str
    admission: dict
    execution: dict
    events: tuple


def canonical_bytes(value):
    """Canonical event material, shared exactly with the paired producer."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise RunEventInvalid("The event material is not valid bounded JSON.") from exc


def event_digest(run_id, admission, execution, event):
    """SHA256 of immutable admission, ticket and event, excluding its digest."""
    body = {key: value for key, value in event.items() if key != "digest"}
    material = {"schema_version": 1, "run_id": run_id, "admission": admission,
                "execution": execution, "event": body}
    return hashlib.sha256(canonical_bytes(material)).hexdigest()


def _number(value, *, positive=False):
    return type(value) is int and int(positive) <= value < MAX_NUMBER


def _token(value):
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _digest(value):
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _time(value):
    if type(value) is not str or not 1 <= len(value) <= 40:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None
    except (ValueError, TypeError, OverflowError):
        return False


def _text(value, size, *, blank=False):
    return (type(value) is str and int(not blank) <= len(value) <= size
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _scope(value):
    # Value meaning belongs to the backend's admitted/ticket helper. Here the
    # complete object must be JSON-safe; exact equality is checked under lock.
    if not isinstance(value, dict) or set(value) != _SCOPE:
        return False
    return all(item is None or _number(item) or _text(item, 255, blank=True)
               for item in value.values())


def _admission(value):
    fields = {"schema_version", "dispatch_key", "organization_id", "principal", "workflow", "domain",
              "github_repo", "request_digest", "scope", "event_protocol"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["event_protocol"]) is not int or value["event_protocol"] != 1
            or not _text(value["dispatch_key"], 255) or not _text(value["organization_id"], 100)
            or not _text(value["workflow"], 50) or not _text(value["domain"], 255, blank=True)
            or not _text(value["github_repo"], 255, blank=True) or not _digest(value["request_digest"])
            or not _scope(value["scope"])):
        return False
    principal = value["principal"]
    if not isinstance(principal, dict):
        return False
    if principal.get("kind") == "user":
        return (set(principal) == {"kind", "user_id", "actor_id"}
                and _text(principal["user_id"], 100) and _text(principal["actor_id"], 100, blank=True))
    return (set(principal) == {"kind", "service"} and principal.get("kind") == "scheduler"
            and _token(principal["service"]))


def _execution(value):
    return (isinstance(value, dict) and set(value) == {"schema_version", "execution_id", "generation",
            "admission_digest", "request_digest", "scope"} and type(value["schema_version"]) is int
            and value["schema_version"] == 1 and _token(value["execution_id"])
            and _number(value["generation"]) and _digest(value["admission_digest"])
            and _digest(value["request_digest"]) and _scope(value["scope"]))


def _failure(raw):
    allowed = {"code", "stage", "registry_version", "registry_sha256"}
    if (not isinstance(raw, dict) or not {"code", "stage"} <= set(raw) <= allowed
            or not _token(raw["code"]) or not _token(raw["stage"])
            or ("registry_version" in raw and not _number(raw["registry_version"], positive=True))
            or ("registry_sha256" in raw and not _digest(raw["registry_sha256"]))):
        raise RunEventInvalid("The failure observation has an invalid shape.")
    # Membership and local classification, never a sender's hash, version,
    # class, message, retry policy, owner action or unrecognized raw string.
    from .website_failure_contract import classify_failure
    return classify_failure(raw["code"], raw["stage"])


def _event(raw, run_id, admission, execution):
    if not isinstance(raw, dict) or type(raw.get("type")) is not str or raw["type"] not in EVENT_TYPES:
        raise RunEventInvalid("The event type is not supported.")
    required, optional = _FIELDS[raw["type"]]
    if (not _BASE | required <= set(raw) or not set(raw) <= _BASE | required | optional
            or any(not _number(raw.get(field), positive=field == "seq") for field in ("seq", "generation", "state_version"))
            or raw["generation"] != execution["generation"] or not _time(raw.get("at"))
            or not _digest(raw.get("digest"))):
        raise RunEventInvalid("The event has an invalid typed shape.")
    if len(canonical_bytes(raw)) > MAX_EVENT_BYTES:
        raise RunEventInvalid("The event exceeds the bounded protocol size.")
    if not hmac.compare_digest(raw["digest"], event_digest(run_id, admission, execution, raw)):
        raise RunEventInvalid("The event content digest does not match.")
    if any(field in raw and not _token(raw[field]) for field in ("step", "kind", "artifact_id", "model")):
        raise RunEventInvalid("The event identifier is invalid.")
    kind = raw["type"]
    if kind in {"step_started", "step_done"} and (not _number(raw["attempt"], positive=kind == "step_started")
                                                  or raw["attempt"] > MAX_ATTEMPT):
        raise RunEventInvalid("The step attempt is invalid.")
    if kind == "run_state" and (type(raw["status"]) is not str or raw["status"] not in RUN_STATUSES):
        raise RunEventInvalid("The execution status is invalid.")
    if kind == "step_done" and (type(raw["status"]) is not str or raw["status"] not in STEP_DONE):
        raise RunEventInvalid("The step status is invalid.")
    if kind == "artifact" and "content_sha256" in raw and not _digest(raw["content_sha256"]):
        raise RunEventInvalid("The artifact digest is invalid.")
    facts = deepcopy(raw)
    if kind == "cost":
        if not (_COUNTS | {"usd"}) & set(raw) or any(field in raw and not _number(raw[field]) for field in _COUNTS):
            raise RunEventInvalid("The cost observation has invalid counts.")
        if "usd" in raw:
            try:
                amount = Decimal(raw["usd"]) if type(raw["usd"]) is str and len(raw["usd"]) <= 40 else None
            except InvalidOperation:
                amount = None
            if amount is None or not amount.is_finite() or amount < 0:
                raise RunEventInvalid("The cost observation has an invalid amount.")
    if kind == "failure":
        facts["failure"] = _failure(raw["failure"])
    return {"event": deepcopy(raw), "facts": facts, "digest": raw["digest"],
            "execution_id": execution["execution_id"], "execution": deepcopy(execution)}


def validate_authoritative_envelope(payload):
    """Validate before binding; this syntactic check never grants admission."""
    required = {"event", "schema_version", "run_id", WIRE_ADMISSION, WIRE_EXECUTION, "events"}
    optional = {"event_type", "event_id", "emitted_at"}
    if (not isinstance(payload, dict) or not required <= set(payload) <= required | optional
            or payload["event"] != EVENT_NAME or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1 or not _text(payload["run_id"], 100)
            or not _admission(payload[WIRE_ADMISSION]) or not _execution(payload[WIRE_EXECUTION])
            or not isinstance(payload["events"], list) or not 0 < len(payload["events"]) <= MAX_BATCH
            or ("event_type" in payload and payload["event_type"] != EVENT_NAME)
            or ("event_id" in payload and not _text(payload["event_id"], 100))
            or ("emitted_at" in payload and not _time(payload["emitted_at"]))):
        raise RunEventInvalid("The authoritative event envelope is invalid.")
    run_id = payload["run_id"]
    admission, execution = payload[WIRE_ADMISSION], payload[WIRE_EXECUTION]
    events = tuple(_event(raw, run_id, admission, execution) for raw in payload["events"])
    return ValidatedBatch(run_id, deepcopy(admission), deepcopy(execution), events)


def _event_key(receipt):
    event = receipt["event"]
    return json.dumps([receipt["execution_id"], event["generation"], event["seq"]], separators=(",", ":"))


def fold_authoritative_events(saved, batch, *, current_execution_id, local_status="queued", local_step="", project_current=True):
    """Append once, retain gaps and project only a backend-current ticket.

    This pure reducer returns isolated JSON plus accepted/duplicate counts. A
    receiver must check the real admission/ticket under lock before persisting.
    """
    if saved is None:
        ledger = {"schema_version": 1, "admission": deepcopy(batch.admission), "events": {}, "projection": {},
                  "closed_runs": {}, "closed_attempts": {}, "attempt_ordinals": {}}
    elif (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
            or saved["schema_version"] != 1 or not isinstance(saved.get("events"), dict)
            or canonical_bytes(saved.get("admission")) != canonical_bytes(batch.admission)):
        raise RunEventConflict("The retained event ledger cannot be replaced.")
    else:
        ledger = deepcopy(saved)
    for key, receipt in ledger["events"].items():
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("event"), dict)
                or not isinstance(receipt.get("facts"), dict) or not _execution(receipt.get("execution"))
                or receipt.get("execution_id") != receipt["execution"]["execution_id"]
                or not _digest(receipt.get("digest"))):
            raise RunEventConflict("The retained event history has an invalid receipt.")
        try:
            matches = key == _event_key(receipt) and hmac.compare_digest(receipt["digest"],
                event_digest(batch.run_id, batch.admission, receipt["execution"], receipt["event"]))
        except (KeyError, TypeError, RunEventInvalid):
            matches = False
        if not matches:
            raise RunEventConflict("The retained event history has changed immutable content.")
    for field in ("closed_runs", "closed_attempts", "attempt_ordinals"):
        if not isinstance(ledger.get(field), dict):
            raise RunEventConflict("The retained event ledger has invalid projection metadata.")
    accepted = duplicates = 0
    for receipt in sorted(batch.events, key=lambda item: (item["event"]["state_version"], item["event"]["seq"])):
        key = _event_key(receipt)
        previous = ledger["events"].get(key)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get("digest") != receipt["digest"]:
                raise RunEventConflict("An immutable event identity has different content.")
            duplicates += 1
        else:
            ledger["events"][key] = deepcopy(receipt)
            event = receipt["event"]
            execution = receipt["execution_id"]
            if event["type"] == "run_state" and event["status"] in TERMINAL:
                ledger["closed_runs"].setdefault(execution, key)
            if event["type"] == "step_done":
                attempt_key = json.dumps([execution, event["generation"], event["step"], event["attempt"]])
                ledger["closed_attempts"].setdefault(attempt_key, key)
            accepted += 1
    if not accepted or batch.execution["execution_id"] != current_execution_id or not project_current:
        return ledger, accepted, duplicates
    current = sorted(((key, item["facts"]) for key, item in ledger["events"].items()
                      if item["execution_id"] == current_execution_id),
                     key=lambda item: (item[1]["state_version"], item[1]["seq"]))
    projection = {"execution_id": current_execution_id, "generation": batch.execution["generation"],
                  "status": None, "current_step": "", "steps": {},
                  "attempts": {}, "cost": {}, "failure": None, "order": None}
    stopped = False
    for key, event in current:
        kind = event["type"]
        order = [event["state_version"], event["seq"]]
        if kind == "failure":
            projection["failure"] = deepcopy(event["failure"])
        elif kind == "cost":
            projection["cost"][event.get("step", "_run")] = {
                field: event[field] for field in (*_COUNTS, "usd", "model") if field in event}
        elif not stopped and kind == "run_state":
            if event["status"] in TERMINAL and ledger["closed_runs"].get(current_execution_id) != key:
                continue
            projection["status"] = event["status"]
            projection["current_step"] = event.get("step", projection["current_step"])
            projection["order"] = order
            stopped = event["status"] in TERMINAL
        elif not stopped and kind in {"step_started", "step_done"}:
            step, attempt = event["step"], event["attempt"]
            attempt_key = json.dumps([current_execution_id, event["generation"], step, attempt])
            if kind == "step_done" and ledger["closed_attempts"].get(attempt_key) != key:
                continue
            attempts = projection["attempts"].setdefault(step, {})
            previous = attempts.get(str(attempt))
            # An attempt cannot return to running after its first final fact.
            if previous and previous["status"] in STEP_DONE:
                continue
            state = {"status": "running" if kind == "step_started" else event["status"],
                     "attempt": attempt, "order": order, "at": event["at"],
                     "started_at": previous.get("started_at") if previous else None,
                     "completed_at": event["at"] if kind == "step_done" else None}
            if kind == "step_started" and state["started_at"] is None:
                state["started_at"] = event["at"]
            attempts[str(attempt)] = state
            latest = projection["steps"].get(step)
            if latest is None or attempt >= latest["attempt"]:
                projection["steps"][step] = deepcopy(state)
                projection["current_step"] = step
                projection["order"] = order
    if local_status in HARD_TERMINAL:
        projection["status"], projection["current_step"] = local_status, local_step
        projection["failure"] = None
    ledger["projection"] = projection
    return ledger, accepted, duplicates


def _lifecycle(projection):
    return {key: projection.get(key) for key in ("execution_id", "status", "current_step", "steps", "attempts", "order")}


def event_attempt_identity(run, step_key, ordinal):
    """Compatibility ordinal metadata; never a CF retry or spend counter."""
    from .run_ownership import EVENT_RESULT_KEY
    ledger = (run.result or {}).get(EVENT_RESULT_KEY, {})
    mapping = ledger.get("attempt_ordinals", {}) if isinstance(ledger, dict) else {}
    for encoded, number in mapping.items():
        if number != ordinal:
            continue
        try:
            execution, generation, step, reported = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if step == step_key:
            return {"execution_id": execution, "generation": generation, "reported_attempt": reported}
    return None


def _project_steps(run, ledger, projection):
    from workflow_runs.models import ContentFactoryRunStep, ContentFactoryRunStepAttempt
    for index, (name, current) in enumerate(projection["steps"].items()):
        step, _ = ContentFactoryRunStep.objects.get_or_create(run=run, step_key=name,
            defaults={"display_order": index})
        # Keep the actual reported attempt semantics; ordinals below are only
        # identities for compatibility history rows, never policy cap inputs.
        stamps = {field: datetime.fromisoformat(current[field].replace("Z", "+00:00")) if current.get(field) else None
                  for field in ("started_at", "completed_at")}
        ContentFactoryRunStep.objects.filter(pk=step.pk).update(status=current["status"], attempts=current["attempt"], **stamps)
        existing = set(step.attempt_history.values_list("attempt", flat=True))
        for reported, state in sorted(projection["attempts"][name].items(), key=lambda item: int(item[0])):
            encoded = json.dumps([projection["execution_id"], projection["generation"], name, int(reported)])
            ordinal = ledger["attempt_ordinals"].get(encoded)
            if ordinal is None:
                allocated = [value for key, value in ledger["attempt_ordinals"].items()
                             if json.loads(key)[2] == name]
                ordinal = max([0, *existing, *allocated]) + 1
                if ordinal > 2_147_483_647:
                    raise RunEventConflict("The compatibility attempt history is full.")
                ledger["attempt_ordinals"][encoded] = ordinal
                existing.add(ordinal)
            attempt = ContentFactoryRunStepAttempt.objects.filter(step=step, attempt=ordinal).first()
            stamps = {field: datetime.fromisoformat(state[field].replace("Z", "+00:00")) if state.get(field) else None
                      for field in ("started_at", "completed_at")}
            if attempt is None:
                ContentFactoryRunStepAttempt.objects.create(step=step, attempt=ordinal, status=state["status"], **stamps)
            elif attempt.status not in STEP_DONE:
                ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk).update(status=state["status"], **stamps)


def receive_authoritative_run_events(payload):
    """Receive authenticated internal events for a pre-existing bound owner.

    The service callback router supplies authentication and safe prior binding.
    Admission/ticket equality is checked again under the canonical Run row lock.
    No external work, money, approval or delivered-content fact is performed.
    """
    from django.db import transaction
    from django.utils import timezone
    from rest_framework.response import Response
    from workflow_runs.models import ContentFactoryRun
    from .run_ownership import (DISPATCH_RESULT_KEY, EVENT_RESULT_KEY, RunOwnershipConflict, current_execution_ticket,
                                require_event_execution, require_event_identity)
    try:
        batch = validate_authoritative_envelope(payload)
    except RunEventInvalid:
        return Response({"error": "run_event_invalid", "retryable": False}, status=400)
    try:
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().filter(run_id=batch.run_id).first()
            if run is None:
                return Response({"status": "pending", "reason": "run_not_found", "retryable": True}, status=409)
            require_event_identity(run, payload)
            require_event_execution(run, payload, generation=batch.execution["generation"])
            current = current_execution_ticket(run)
            result = deepcopy(run.result)
            before = result.get(EVENT_RESULT_KEY)
            previous_projection = before.get("projection", {}) if isinstance(before, dict) else {}
            dispatch = result.get(DISPATCH_RESULT_KEY, {})
            transport_hold = (run.status == "blocked" and run.current_step == "dispatch_pending_resolution"
                and isinstance(dispatch, dict) and dispatch.get("state") == "pending_resolution")
            # A backend stop which differs from the last event projection is
            # authoritative until a new backend-issued execution ticket exists.
            local_stop = (run.status in TERMINAL and run.status not in HARD_TERMINAL and not transport_hold
                and (not previous_projection or previous_projection.get("execution_id") == current["execution_id"])
                and previous_projection.get("status") != run.status)
            ledger, accepted, duplicates = fold_authoritative_events(before, batch,
                current_execution_id=current["execution_id"], local_status=run.status, local_step=run.current_step,
                project_current=not local_stop)
            if accepted:
                projection = ledger["projection"]
                projected = (batch.execution["execution_id"] == current["execution_id"]
                    and run.status not in HARD_TERMINAL and not local_stop
                    and projection.get("order") is not None
                    and _lifecycle(projection) != _lifecycle(previous_projection))
                fields = {}
                if projected:
                    _project_steps(run, ledger, projection)
                    if projection["status"] is not None:
                        fields["status"] = projection["status"]
                    if projection["current_step"]:
                        fields["current_step"] = projection["current_step"]
                    ledger["last_projected_at"] = timezone.now().isoformat()
                if (batch.execution["execution_id"] == current["execution_id"]
                        and run.status not in HARD_TERMINAL and not local_stop):
                    from .website_failure_contract import attach_failure_projection
                    status = projection.get("status")
                    if (projection.get("failure") and status is not None
                            and status not in {"queued", "running", *HARD_TERMINAL}):
                        result.update(attach_failure_projection({}, projection["failure"]))
                    elif status in {"queued", "running", "completed"}:
                        for field in ("failure", "failure_class", "failure_fingerprint", "error_code", "retryable"):
                            result.pop(field, None)
                # Only this backend-private ledger is changed. Compatibility
                # failure/status presentation reads the typed derived projection;
                # a worker cannot merge arbitrary result fields through it.
                result[EVENT_RESULT_KEY] = ledger
                ContentFactoryRun.objects.filter(pk=run.pk).update(result=result, **fields)
            return Response({"status": "received", "accepted": accepted, "duplicates": duplicates,
                             "execution_id": batch.execution["execution_id"]}, status=200)
    except (RunOwnershipConflict, RunEventConflict):
        return Response({"error": "run_event_conflict", "retryable": False}, status=409)
