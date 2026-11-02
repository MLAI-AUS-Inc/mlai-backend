"""Bounded, backend-owned source compare-and-swap for an existing run."""
from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Mapping

from django.conf import settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import p2_foundation, registry_policy
from .website_contract import WebsiteAuthorityError, CONNECTION_FIELDS, connection_contract
from .website_connections import authority_guard, REPOSITORY_WORKFLOWS

SHA = re.compile(r"[a-f0-9]{40}\Z")
REASONS = frozenset({"source_changed_mid_run", "setup_start_moved", "resume_latest"})


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _refuse(*, limit=False):
    return WebsiteAuthorityError(
        "source_repin_limit_reached" if limit else "source_repin_refused",
        "This article cannot refresh its website source again." if limit else "This article cannot refresh its website source.",
        retryable=False,
    )


def repin_limit():
    """Return the registry's bounded source re-pin limit, failing closed."""
    value = registry_policy.limits().get("source_repins_per_run", 2)
    return min(value, 2) if type(value) is int and value >= 0 else 0


def validated_history(value, *, max_repins=2):
    """Validate a bounded backend receipt before trusting its source lineage."""
    receipt = _mapping(value)
    history = receipt.get("history")
    count = receipt.get("repins")
    if (type(receipt.get("version")) is not int or receipt.get("version") != 1 or type(count) is not int or not 1 <= count <= max_repins
            or not isinstance(history, list) or len(history) != count):
        return None
    previous = receipt.get("first_from_sha")
    if not isinstance(previous, str) or not SHA.fullmatch(previous):
        return None
    keys = set()
    for item in history:
        if not isinstance(item, Mapping):
            return None
        target, key = item.get("to_sha"), item.get("repin_key")
        if (item.get("from_sha") != previous or not isinstance(target, str) or not SHA.fullmatch(target)
                or target == previous or not isinstance(key, str) or not key or key in keys
                or not isinstance(item.get("reason"), str) or item["reason"] not in REASONS or not item.get("at")):
            return None
        keys.add(key)
        previous = target
    return receipt if previous == receipt.get("last_to_sha") else None


def _settled(run):
    result, request = _mapping(run.result), _mapping(run.run_request)
    if _mapping(result.get("points_v1")).get("state") in {"refunded", "released"}:
        return True
    from .first_article_entitlement import entitlement_payload
    if entitlement_payload(run).get("entitlementStatus") == "released":
        return True
    from roo.models import Ledger
    from .billing_lineage import original_article_spend_for_run
    original = original_article_spend_for_run(run)
    if original is not None and Ledger.objects.filter(kind="REFUND", refund_of=original).exists():
        return True
    # Re-pin only an original spend that has not been refunded by any path.
    ledger_id = request.get("roo_points_ledger_id") or getattr(run, "billing_ledger_id", None)
    original_key = request.get("original_spend_key") or request.get("roo_points_spend_key")
    if ledger_id and str(ledger_id).isdigit():
        if Ledger.objects.filter(kind="REFUND", refund_of_id=int(ledger_id)).exists():
            return True
    if isinstance(original_key, str) and original_key:
        return Ledger.objects.filter(kind="REFUND", refund_of__idempotency_key=original_key).exists()
    return False


def _precheck(run):
    if (not getattr(settings, "ARTICLES_SOURCE_REPIN_ENABLED", False) or run.organization_id is None
            or not p2_foundation.p2_applies(run.organization) or run.workflow not in REPOSITORY_WORKFLOWS
            or run.status not in {"queued", "running", "failed", "blocked", "awaiting_delivery_mode",
                                  "awaiting_confirmation", "awaiting_approval", "approval_required"} or _settled(run)):
        raise _refuse()


def repin_source(run_id, payload):
    """Re-pin a run only after fresh repository proof and locked scope checks.

    Provider calls happen inside authority_guard's preflight, before any database
    lock. Both the guard and this run compare-and-swap recheck their snapshots.
    No debit, entitlement, connection verification or run timestamp is changed.
    """
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    before, after = payload.get("from_sha"), payload.get("to_sha")
    reason, key = payload.get("reason"), payload.get("repin_key")
    if (not isinstance(before, str) or not SHA.fullmatch(before) or not isinstance(after, str)
            or not SHA.fullmatch(after) or before == after or not isinstance(reason, str) or reason not in REASONS
            or not isinstance(key, str) or not re.fullmatch(re.escape(str(run_id)) + r":[1-9][0-9]{0,4}", key)):
        raise _refuse()
    run = ContentFactoryRun.objects.select_related("organization").filter(run_id=run_id).first()
    if run is None:
        raise ContentFactoryRun.DoesNotExist
    from .run_ownership import owner_receipt
    if (owner_receipt(run) or {}).get("authority", {}).get("event_protocol") == 1:
        return _repin_owned_source(run, payload)
    _precheck(run)
    request = _mapping(run.run_request)
    original_binding = connection_contract(request)
    if not original_binding or not original_binding.get("repository_id"):
        raise _refuse()
    # Never let a service caller substitute another connection or operation.
    for field in (*CONNECTION_FIELDS, "operation_id", "operation_attempt"):
        if request.get(field) is not None and str(payload.get(field, "")) != str(request[field]):
            raise _refuse()
    binding = {**request, **payload, "run_id": run.run_id, "expected_source_sha": after,
               "domain": run.domain, "github_repo": run.github_repo}
    try:
        with authority_guard(binding, action="setup", require_current_head=True) as connection:
            locked = ContentFactoryRun.objects.select_for_update(of=("self",)).select_related("organization").get(pk=run.pk)
            _precheck(locked)
            if connection.organization_id != locked.organization_id:
                raise _refuse()
            request, result = dict(_mapping(locked.run_request)), dict(_mapping(locked.result))
            # Concurrent request replacement is not authorized by earlier provider proof.
            if any(request.get(field) != run.run_request.get(field) for field in (*CONNECTION_FIELDS, "operation_id", "operation_attempt")):
                raise _refuse()
            history = validated_history(result.get("re_pin_v1"))
            if result.get("re_pin_v1") is not None and history is None:
                raise _refuse()
            entries = list(history["history"]) if history else []
            current = request.get("expected_source_sha")
            previous = next((item for item in entries if item["repin_key"] == key), None)
            if previous:
                if (previous["from_sha"] == before and previous["to_sha"] == after
                        and previous["reason"] == reason and current == after):
                    return {"status": "already_repinned", "run_id": run_id, "from_sha": before,
                            "to_sha": after, "repins": len(entries), "max_repins": repin_limit()}
                raise _refuse()
            if len(entries) >= repin_limit():
                raise _refuse(limit=True)
            if current != before or key != f"{run_id}:{len(entries) + 1}":
                raise _refuse()
            entries.append({"from_sha": before, "to_sha": after, "reason": reason, "repin_key": key,
                            "at": timezone.now().isoformat()})
            request["expected_source_sha"] = after
            result["re_pin_v1"] = {"version": 1, "repins": len(entries), "first_from_sha": entries[0]["from_sha"],
                                    "last_to_sha": after, "history": entries}
            ContentFactoryRun.objects.filter(pk=locked.pk).update(run_request=request, result=result)
    except WebsiteAuthorityError as exc:
        if exc.authority_code == "source_repin_limit_reached":
            from .founder_journey_alerts import queue_founder_journey_alert
            queue_founder_journey_alert(organization=run.organization, run=run, alert_reason="source_change_repeated")
        raise
    return {"status": "repinned", "run_id": run_id, "from_sha": before, "to_sha": after,
            "repins": len(entries), "max_repins": repin_limit()}


def _repin_owned_source(run, payload):
    """Refresh a proven source ticket without changing original drafting input."""
    from .run_execution_control import (BUDGET_KEY, ExecutionControlDenied, append_execution_ticket, record_budget_observation,
        current_execution_request, require_current_principal, require_control_budget, valid_control_budget, validate_budget_observation)
    from .run_execution_control_views import _budget, _require_active
    from .run_ownership import (WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, RECONCILIATION_RESULT_KEY,
        current_execution_ticket, owner_receipt, require_admission_identity, require_event_execution, wire_identity)
    from .website_contract import evidence_digest
    from .run_reconciliation import reconcile_authoritative_run
    from organizations.models import Organization
    require_admission_identity(run, payload)
    require_event_execution(run, payload)
    _require_active(run)
    require_current_principal(run)
    require_control_budget(run)
    if not run.github_repo or run.workflow not in REPOSITORY_WORKFLOWS or _settled(run):
        raise _refuse()
    original_budget = _mapping(run.run_request).get(BUDGET_KEY)
    priced = valid_control_budget(original_budget) and original_budget["runtime"] == "maintained_sdk"
    maximum = 1024 if priced else repin_limit()
    request = current_execution_request(run)
    before, after, reason, key = (payload[field] for field in ("from_sha", "to_sha", "reason", "repin_key"))
    command_key = evidence_digest({"reason": "source_repin", "run_pk": run.pk,
                                  "repin_key": key, "from_sha": before, "to_sha": after})
    history = validated_history(_mapping(run.result).get("re_pin_v1"), max_repins=maximum)
    previous = next((item for item in (history or {}).get("history", []) if item["repin_key"] == key), None)
    budget = None
    if previous is None:
        require_event_execution(run, payload, current=True)
        budget = _budget(run.run_id)
        state = reconcile_authoritative_run(run.run_id, refresh=True)
        if state.get("outcome") != "history_received" or state.get("has_more") or state.get("active_claim"):
            raise ExecutionControlDenied("execution_history_pending", pending=True)
    if any(request.get(field) is not None and payload.get(field) != request[field]
           for field in (*CONNECTION_FIELDS, "operation_id", "operation_attempt", "deletion_epoch")):
        raise _refuse()
    binding = {**request, "expected_source_sha": after, "run_id": run.run_id,
               "domain": run.domain, "github_repo": run.github_repo}
    with authority_guard(binding, action="setup", require_current_head=True) as connection:
        Organization.objects.select_for_update().get(pk=run.organization_id)
        locked = ContentFactoryRun.objects.select_for_update(of=("self",)).select_related("organization").get(pk=run.pk)
        require_admission_identity(locked, payload)
        require_event_execution(locked, payload)
        _require_active(locked)
        require_current_principal(locked)
        if _settled(locked):
            raise _refuse()
        current = current_execution_request(locked)
        if connection.organization_id != locked.organization_id or current != request:
            raise _refuse()
        result = deepcopy(_mapping(locked.result))
        history = validated_history(result.get("re_pin_v1"), max_repins=maximum)
        if result.get("re_pin_v1") is not None and history is None:
            raise _refuse()
        entries = list((history or {}).get("history", []))
        previous = next((item for item in entries if item["repin_key"] == key), None)
        if previous is not None:
            if previous["from_sha"] != before or previous["to_sha"] != after or previous["reason"] != reason:
                raise _refuse()
            status = "already_repinned"
        else:
            require_event_execution(locked, payload, current=True)
            cursor = validate_budget_observation(locked, budget)
            metadata = result.get(RECONCILIATION_RESULT_KEY, {})
            if metadata.get("cursor") != cursor or metadata.get("has_more") is True or metadata.get("claim"):
                raise ExecutionControlDenied("execution_history_pending", pending=True)
            if len(entries) >= maximum:
                raise _refuse(limit=True)
            if current.get("expected_source_sha") != before or key != f"{run.run_id}:{len(entries) + 1}":
                raise _refuse()
            record_budget_observation(locked, budget)
            result = deepcopy(locked.result)
            entries.append({"from_sha": before, "to_sha": after, "reason": reason, "repin_key": key,
                            "at": timezone.now().isoformat()})
            status = "repinned"
            result["re_pin_v1"] = {"version": 1, "repins": len(entries), "first_from_sha": entries[0]["from_sha"],
                                   "last_to_sha": after, "history": entries}
            ContentFactoryRun.objects.filter(pk=locked.pk).update(result=result)
            locked.result = result
        scope = {**current_execution_ticket(locked)["scope"], "expected_source_sha": after}
        ticket = append_execution_ticket(locked, control_key=command_key, reason="source_repin", scope=scope,
            increment_generation=False, expected_execution_id=payload[WIRE_EXECUTION_KEY]["execution_id"])
        return {"status": status, "run_id": run.run_id, "from_sha": before, "to_sha": after,
                "repins": len(entries), "max_repins": maximum, "schema_version": 1,
                WIRE_REQUEST_KEY: wire_identity(owner_receipt(locked)), WIRE_EXECUTION_KEY: ticket}
