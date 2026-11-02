"""Pull immutable history for explicitly admitted authoritative executions.

This owner is independent of new-admission switches. It neither reads a
mutable status snapshot nor starts, approves, cancels or settles work. A
short database claim fences every response; network I/O happens outside it.
"""
from copy import deepcopy
from datetime import timedelta, timezone as datetime_timezone
import json
from urllib.parse import quote
from uuid import uuid4
from time import monotonic

import requests
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .run_event_authority import RunEventInvalid, validate_authoritative_envelope, receive_authoritative_run_events
from .run_ownership import (
    DISPATCH_RESULT_KEY, EVENT_RESULT_KEY, OWNER_RESULT_KEY, RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    RunOwnershipConflict, bind_admitted_run, current_execution_ticket, owner_receipt,
    require_event_execution, require_event_identity, wire_identity,
)
from .run_status_contract import PLATFORM_EXTENSIONS, RUN_STATUSES, TERMINAL
from .website_contract import evidence_digest

NONTERMINAL_STATUSES = (RUN_STATUSES | PLATFORM_EXTENSIONS) - TERMINAL
PAGE_LIMIT = 100
MAX_PAGE_BYTES = 8 * 1024 * 1024
LEASE_SECONDS = 60
POLL_SECONDS = 120
MAX_RETRY_SECONDS = 3600
MAX_HTTP_SECONDS = 15
MAX_SWEEP_SECONDS = 45


class RunReconciliationError(ValueError):
    """A history page cannot safely advance this execution's retained cursor."""


def authoritative_run(run):
    """True only for an explicit backend admission owning protocol version one."""
    receipt = owner_receipt(run)
    authority = receipt.get("authority", {}) if receipt else {}
    return type(authority.get("event_protocol")) is int and authority["event_protocol"] == 1


def legacy_event_rows():
    """SQL predicate retaining absent legacy keys while excluding event owners."""
    path = f"result__{OWNER_RESULT_KEY}__authority__event_protocol"
    return Q(**{path + "__isnull": True}) | ~Q(**{path: 1})


def _stamp(now):
    return now.astimezone(datetime_timezone.utc).isoformat()


def _time(value):
    try:
        parsed = parse_datetime(value) if type(value) is str else None
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def _metadata(run):
    result = run.result if isinstance(run.result, dict) else {}
    saved = result.get(RECONCILIATION_RESULT_KEY)
    return deepcopy(saved) if isinstance(saved, dict) else {}


def _cursor(meta):
    value = meta.get("cursor", 0)
    if type(value) is not int or not 0 <= value < 2 ** 53:
        raise RunReconciliationError("invalid_local_cursor")
    return value


def _pending(run):
    receipt = owner_receipt(run)
    state = (run.result or {}).get(DISPATCH_RESULT_KEY, {})
    if not receipt or not isinstance(state, dict):
        return False
    if state.get("state") == "pending_resolution":
        return True
    # Process death can follow reservation before the POST or its uncertain
    # response is recorded. The original admission still owns that exact key.
    return (state.get("state") != "bound"
        and run.run_id == receipt.get("reservation_run_id", receipt["identity"]["dispatch_key"])
        and not (run.result or {}).get(EVENT_RESULT_KEY, {}).get("events"))


def _grace_deadline(run):
    from .vibe_marketing_views import CONTENT_FACTORY_DISPATCH_ABSENT_GRACE_SECONDS
    return run.created_at + timedelta(seconds=CONTENT_FACTORY_DISPATCH_ABSENT_GRACE_SECONDS)


def _eligible(run):
    return authoritative_run(run) and (run.status in NONTERMINAL_STATUSES or _pending(run)
                                      or _metadata(run).get("has_more") is True)


def _write_meta(run, meta, *, extra_result=None):
    from workflow_runs.models import ContentFactoryRun
    result = {**run.result, **(extra_result or {}), RECONCILIATION_RESULT_KEY: meta}
    # Preserve the original execution, source, event-watermark and legacy
    # reconciliation clocks. This is a transport attempt, not source progress.
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result


def _claim(pk, now, *, refresh=False):
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(pk=pk).first()
        if run is None or not authoritative_run(run) or (not refresh and not _eligible(run)):
            return None
        meta = _metadata(run)
        due = _time(meta.get("next_at"))
        if due is not None and due > now and (meta.get("claim") or not refresh):
            return None
        try:
            cursor = _cursor(meta)
            ticket = current_execution_ticket(run)
            # Revalidate the full stored tuple, not a worker-looking marker.
            echo = {WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: ticket}
            require_event_identity(run, echo)
            require_event_execution(run, echo, current=True)
        except (RunOwnershipConflict, RunReconciliationError, TypeError, KeyError):
            # A damaged owner cannot consume every bounded tick ahead of
            # healthy executions. Keep its cursor and history unchanged.
            meta.update(last_attempt_at=_stamp(now), last_outcome="admission_invalid",
                        next_at=_stamp(now + timedelta(seconds=MAX_RETRY_SECONDS)))
            _write_meta(run, meta)
            return {"invalid": True}
        claim = {"nonce": uuid4().hex, "run_id": run.run_id, "cursor": cursor,
                 "at": _stamp(now), "execution_id": ticket["execution_id"]}
        meta.update(schema_version=1, claim=claim, last_attempt_at=_stamp(now),
                    next_at=_stamp(now + timedelta(seconds=LEASE_SECONDS)), last_outcome="claimed")
        _write_meta(run, meta)
        return {"pk": run.pk, **claim, "pending_dispatch": _pending(run),
                "dispatch_key": owner_receipt(run)["identity"]["dispatch_key"]}


def _locked_claim(claim):
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.select_for_update().filter(pk=claim["pk"]).first()
    meta = _metadata(run) if run is not None else {}
    saved = meta.get("claim", {})
    if (run is None or not authoritative_run(run) or not isinstance(saved, dict)
            or saved.get("nonce") != claim["nonce"] or run.run_id != claim["run_id"]
            or saved.get("cursor") != claim["cursor"] or _cursor(meta) != claim["cursor"]):
        return None, None
    return run, meta


def _finish(claim, *, outcome, now, retry=False):
    with transaction.atomic():
        run, meta = _locked_claim(claim)
        if run is None:
            return "superseded"
        errors = meta.get("errors", 0)
        errors = errors if type(errors) is int and 0 <= errors <= 30 else 0
        errors = min(errors + 1, 30) if retry else 0
        delay = min(MAX_RETRY_SECONDS, 15 * 2 ** min(errors, 8)) if retry else POLL_SECONDS
        meta.update(last_outcome=outcome, errors=errors, next_at=_stamp(now + timedelta(seconds=delay)))
        meta.pop("claim", None)
        _write_meta(run, meta)
        return outcome


def validate_history_page(payload, *, run_id, after_seq, limit=PAGE_LIMIT):
    """Validate a bounded immutable page before any receipt or cursor is applied.

    The producer's cursor is a durable global journal position across tickets,
    never a wall clock or a per-generation counter. Reserved sequence gaps
    remain gaps in the event ledger; this cursor is not an interval receipt.
    """
    if (type(after_seq) is not int or not 0 <= after_seq < 2 ** 53
            or type(limit) is not int or not 1 <= limit <= PAGE_LIMIT
            or not isinstance(payload, dict) or set(payload) != {"schema_version", "run_id", "batches", "next_cursor", "has_more"}
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("run_id") != run_id or type(payload.get("has_more")) is not bool
            or type(payload.get("next_cursor")) is not int
            or not after_seq <= payload["next_cursor"] < 2 ** 53
            or not isinstance(payload.get("batches"), list) or len(payload["batches"]) > limit):
        raise RunReconciliationError("invalid_history_page")
    previous, count = after_seq, 0
    batches = []
    try:
        for raw in payload["batches"]:
            batch = validate_authoritative_envelope(raw)
            if batch.run_id != run_id:
                raise RunReconciliationError("history_run_mismatch")
            for row in batch.events:
                seq = row["event"]["seq"]
                if seq <= previous:
                    raise RunReconciliationError("history_cursor_order")
                previous = seq
                count += 1
            batches.append((deepcopy(raw), batch))
    except RunEventInvalid as exc:
        raise RunReconciliationError("invalid_history_event") from exc
    if count > limit or payload["next_cursor"] != previous or (payload["has_more"] and not count):
        raise RunReconciliationError("history_cursor_mismatch")
    return batches


def _fetch_page(base_url, headers, claim):
    if connection.in_atomic_block:
        raise RunReconciliationError("http_under_database_lock")
    deadline = monotonic() + MAX_HTTP_SECONDS
    response = requests.get(f"{base_url}/api/runs/{quote(claim['run_id'], safe='')}/events",
        params={"after_seq": claim["cursor"], "limit": PAGE_LIMIT}, headers=headers,
        timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RunReconciliationError("history_unavailable")
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65_536):
            if monotonic() > deadline:
                raise RunReconciliationError("history_read_deadline")
            raw.extend(chunk)
            if len(raw) > MAX_PAGE_BYTES:
                raise RunReconciliationError("history_page_too_large")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RunReconciliationError("invalid_history_json") from exc
    finally:
        response.close()


def _apply_page(claim, payload, now):
    batches = validate_history_page(payload, run_id=claim["run_id"], after_seq=claim["cursor"])
    with transaction.atomic():
        run, meta = _locked_claim(claim)
        if run is None:
            return "superseded"
        for raw, batch in batches:
            require_event_identity(run, raw)
            require_event_execution(run, raw, generation=batch.execution["generation"])
        for raw, _ in batches:
            response = receive_authoritative_run_events(raw)
            if response.status_code != 200:
                # Roll back the page as a unit; immutable receiver replay would
                # also safely recover a process loss before cursor persistence.
                raise RunReconciliationError("history_event_conflict")
        run.refresh_from_db()
        meta.update(cursor=payload["next_cursor"], has_more=payload["has_more"], errors=0, last_outcome="history_received",
                    next_at=_stamp(now if payload["has_more"] else now + timedelta(seconds=POLL_SECONDS)))
        meta.pop("claim", None)
        _write_meta(run, meta)
    return "history_received"


def _lookup(claim):
    if connection.in_atomic_block:
        raise RunReconciliationError("http_under_database_lock")
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key
    return _lookup_content_factory_dispatch_by_key(_content_factory_remote_config(), claim["dispatch_key"])


def _apply_lookup(claim, outcome, payload, now):
    with transaction.atomic():
        run, meta = _locked_claim(claim)
        if run is None:
            return "superseded"
        ticket = current_execution_ticket(run)
        if ticket["execution_id"] != claim["execution_id"]:
            raise RunReconciliationError("dispatch_ticket_changed")
        body = payload if isinstance(payload, dict) else {}
        if body.get("client_request_id") not in (None, "", claim["dispatch_key"]):
            raise RunReconciliationError("dispatch_key_mismatch")
        dispatch = (run.result or {}).get(DISPATCH_RESULT_KEY, {})
        dispatch = deepcopy(dispatch) if isinstance(dispatch, dict) else {}
        meta.pop("current_no_effect_id", None)
        if outcome == "started" and type(body.get("run_id")) is str:
            # Existing admission owner preserves the canonical PK, original
            # spend/source, history and clocks; collisions remain unbound.
            run = bind_admitted_run(run, body["run_id"])
            dispatch.update(state="bound", key_outcome="started", remote_run_id=run.run_id)
            label = "dispatch_bound"
        else:
            label = "dispatch_pending"
            # Errors, time, generic 404s and mid-claim records never prove no
            # effects. Only the existing authenticated original-key lookup can.
            if (_pending(run) and outcome in {"not_received", "received_not_started", "rejected"}
                    and now >= _grace_deadline(run)
                    and not (run.result or {}).get(EVENT_RESULT_KEY, {}).get("events")):
                proof = {"schema_version": 1, "proof_id": claim["nonce"], "dispatch_key": claim["dispatch_key"],
                    "outcome": outcome, "checked_at": _stamp(now), "execution_id": ticket["execution_id"],
                    "execution_digest": evidence_digest(ticket),
                    "admission_digest": evidence_digest(wire_identity(owner_receipt(run)))}
                proofs = meta.setdefault("no_effect_proofs", {})
                if not isinstance(proofs, dict) or claim["nonce"] in proofs:
                    raise RunReconciliationError("no_effect_receipt_conflict")
                proofs[claim["nonce"]] = proof
                meta["current_no_effect_id"] = claim["nonce"]
                label = "dispatch_no_effect_proven"
            if _pending(run):
                dispatch["state"] = "pending_resolution"
            dispatch.update(key_outcome=outcome if outcome in {"not_received", "received_not_started", "rejected", "unknown"} else "unknown")
        previous_errors = meta.get("errors", 0)
        previous_errors = previous_errors if type(previous_errors) is int and 0 <= previous_errors <= 30 else 0
        errors = 0 if label != "dispatch_pending" else min(previous_errors + 1, 30)
        meta.update(last_outcome=label, errors=errors,
                    next_at=_stamp(now + timedelta(seconds=POLL_SECONDS if not errors else min(MAX_RETRY_SECONDS, 15 * 2 ** min(errors, 8)))))
        meta.pop("claim", None)
        _write_meta(run, meta, extra_result={DISPATCH_RESULT_KEY: dispatch})
        return label


def no_effect_dispatch_proof(run):
    """Read a current, grace-qualified handoff for the original-charge owner.

    This is not a refund instruction. That controller still verifies the exact
    original ledger, entitlement, retained authority and absence of effects.
    """
    meta = _metadata(run)
    proofs = meta.get("no_effect_proofs", {})
    proof = proofs.get(meta.get("current_no_effect_id")) if isinstance(proofs, dict) else None
    if not authoritative_run(run) or not _pending(run) or not isinstance(proof, dict):
        return None
    ticket = current_execution_ticket(run)
    receipt = owner_receipt(run)
    checked = _time(proof.get("checked_at"))
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("proof_id") != meta.get("current_no_effect_id")
            or proof.get("dispatch_key") != receipt["identity"]["dispatch_key"]
            or proof.get("execution_id") != ticket["execution_id"]
            or proof.get("execution_digest") != evidence_digest(ticket)
            or proof.get("admission_digest") != evidence_digest(wire_identity(receipt))
            or proof.get("outcome") not in {"not_received", "received_not_started", "rejected"}
            or checked is None or checked < _grace_deadline(run)
            or proof.get("checked_at") != meta.get("last_attempt_at")
            or meta.get("last_outcome") != "dispatch_no_effect_proven"
            or (run.result or {}).get(EVENT_RESULT_KEY, {}).get("events")):
        return None
    return deepcopy(proof)


def _process_claim(claim, base_url, headers, now):
    if claim["pending_dispatch"]:
        return _apply_lookup(claim, *_lookup(claim), now)
    payload = _fetch_page(base_url, headers, claim)
    return _apply_page(claim, payload, now) if payload is not None else _apply_lookup(claim, *_lookup(claim), now)


def _read_state(pk, *, outcome, now):
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.filter(pk=pk).first()
    if run is None:
        return {"status": "pending", "outcome": "run_not_found"}
    meta = _metadata(run)
    due = _time(meta.get("next_at"))
    active = bool(meta.get("claim") and due and due > now)
    try:
        cursor = _cursor(meta)
        ticket_id = current_execution_ticket(run)["execution_id"]
    except (RunOwnershipConflict, RunReconciliationError, KeyError, TypeError):
        cursor, ticket_id = None, None
    more = meta.get("has_more") is True
    ready = outcome == "history_received" and cursor is not None and ticket_id is not None and not more and not active
    return {"status": "completed" if ready else "pending",
            "outcome": outcome, "run_id": run.run_id, "cursor": cursor, "has_more": more,
            "active_claim": active, "current_execution_id": ticket_id}


def reconcile_authoritative_run(run_id, *, now=None, refresh=False):
    """Pull one admitted Run for a trusted control caller, outside all SQL locks.

    ``refresh=True`` bypasses cadence, never another unexpired nonce. This
    result is transport evidence only; a grant must re-lock and compare the
    exact current ticket, projection, drained cursor and CF budget anchor.
    """
    from workflow_runs.models import ContentFactoryRun
    from .reconciliation import _remote_base_url, _remote_headers
    if connection.in_atomic_block:
        raise RunReconciliationError("http_under_database_lock")
    now = now or timezone.now()
    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    if run is None:
        return {"status": "pending", "outcome": "run_not_found"}
    if not authoritative_run(run):
        return {"status": "pending", "outcome": "legacy_run"}
    base_url, headers = _remote_base_url(), _remote_headers()
    if not base_url or not headers.get("X-API-KEY"):
        return _read_state(run.pk, outcome="remote_not_configured", now=now)
    claim = _claim(run.pk, now, refresh=refresh is True)
    if claim is None:
        return _read_state(run.pk, outcome="not_due", now=now)
    if claim.get("invalid"):
        return _read_state(run.pk, outcome="admission_invalid", now=now)
    try:
        outcome = _process_claim(claim, base_url, headers, now)
    except (RunReconciliationError, RunOwnershipConflict, requests.RequestException, ValueError, TypeError, KeyError):
        outcome = _finish(claim, outcome="history_unavailable", now=now, retry=True)
    return _read_state(run.pk, outcome=outcome, now=now)


def run_authoritative_reconciliation_sweep(*, limit=10, now=None):
    """Fair bounded pull for every admitted event-owned nonterminal workflow."""
    from workflow_runs.models import ContentFactoryRun
    from .reconciliation import _remote_base_url, _remote_headers
    now = now or timezone.now()
    limit = limit if type(limit) is int and 1 <= limit <= 100 else 10
    summary = {"status": "completed", "checked": 0, "history_received": 0,
               "dispatch_bound": 0, "dispatch_pending": 0, "dispatch_no_effect_proven": 0,
               "superseded": 0, "errors": 0}
    base_url, headers = _remote_base_url(), _remote_headers()
    if not base_url or not headers.get("X-API-KEY"):
        return {**summary, "skipped": "remote_not_configured"}
    prefix = f"result__{RECONCILIATION_RESULT_KEY}"
    candidates = ContentFactoryRun.objects.filter(
        **{f"result__{OWNER_RESULT_KEY}__schema_version": 1,
           f"result__{OWNER_RESULT_KEY}__identity__schema_version": 1,
           f"result__{OWNER_RESULT_KEY}__authority__backend_facts": True,
           f"result__{OWNER_RESULT_KEY}__authority__event_protocol": 1}).filter(
        Q(status__in=sorted(NONTERMINAL_STATUSES)) | Q(**{f"result__{DISPATCH_RESULT_KEY}__state": "pending_resolution"})
        | Q(**{prefix + "__has_more": True})).filter(
        Q(**{prefix + "__next_at__isnull": True}) | Q(**{prefix + "__next_at__lte": _stamp(now)})).order_by(
        F(prefix + "__last_attempt_at").asc(nulls_first=True), "created_at", "pk")
    identifiers = list(candidates.values_list("pk", flat=True)[:limit])
    deadline = monotonic() + MAX_SWEEP_SECONDS
    for pk in identifiers:
        if monotonic() >= deadline:
            break
        claim = None
        try:
            claim = _claim(pk, now)
            if claim is None:
                continue
            summary["checked"] += 1
            if claim.get("invalid"):
                summary["errors"] += 1
                continue
            outcome = _process_claim(claim, base_url, headers, now)
            summary[outcome] += 1
        except (RunReconciliationError, RunOwnershipConflict, requests.RequestException, ValueError, TypeError, KeyError):
            summary["errors"] += 1
            if claim is not None:
                _finish(claim, outcome="history_unavailable", now=now, retry=True)
    return summary
