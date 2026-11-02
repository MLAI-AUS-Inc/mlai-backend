"""Service-only continuation tickets for existing admitted executions."""
from urllib.parse import quote
import json

import requests
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from workflow_runs.models import ContentFactoryRun
from .run_execution_control import (
    AUTOMATIC_REASONS, CONTROL_KEY, ExecutionControlDenied, _KEY,
    append_execution_ticket, current_execution_request, require_current_principal, record_budget_observation,
    validate_budget_observation,
)
from .run_ownership import (
    WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, RunOwnershipConflict, current_execution_ticket,
    owner_receipt, require_admission_identity, require_event_execution, wire_identity,
)


def _body(payload):
    keys = {"schema_version", "control_key", "reason", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}
    if (not isinstance(payload, dict) or set(payload) != keys
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or type(payload.get("control_key")) is not str or not _KEY.fullmatch(payload["control_key"])
            or type(payload.get("reason")) is not str or payload["reason"] not in AUTOMATIC_REASONS):
        raise ExecutionControlDenied()


def _budget(run_id):
    from django.db import connection
    if connection.in_atomic_block:
        raise ExecutionControlDenied("execution_history_pending", pending=True)
    from .reconciliation import _remote_base_url, _remote_headers
    base, headers = _remote_base_url(), _remote_headers()
    if not base or not headers.get("X-API-KEY"):
        raise ExecutionControlDenied("execution_budget_unavailable", pending=True)
    response = requests.get(f"{base}/api/runs/{quote(run_id, safe='')}/execution-budget",
        headers=headers, timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            raise ExecutionControlDenied("execution_budget_unavailable", pending=True)
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=16_384):
            raw.extend(chunk)
            if len(raw) > 131_072:
                raise ExecutionControlDenied("execution_budget_unavailable")
        return json.loads(raw)
    except ExecutionControlDenied:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExecutionControlDenied("execution_budget_unavailable") from exc
    finally:
        response.close()


def _require_active(run):
    receipt = owner_receipt(run)
    if (receipt is None or receipt["authority"].get("event_protocol") != 1
            or run.status not in {"queued", "running"}
            or run.approval_state not in {"not_required", "approved"}):
        raise ExecutionControlDenied("execution_not_active")
    # A worker cannot turn a needs-user/unsupported stop into a new retry by
    # leaving its mutable lifecycle label as running.
    from .run_ownership import EVENT_RESULT_KEY
    projection = (run.result or {}).get(EVENT_RESULT_KEY, {}).get("projection", {})
    failures = [(run.result or {}).get("failure")]
    if projection.get("execution_id") == current_execution_ticket(run)["execution_id"]:
        failures.append(projection.get("failure"))
    if any(isinstance(failure, dict) and failure.get("failure_class") in {"needs_user", "unsupported", "waiting"}
           for failure in failures):
        raise ExecutionControlDenied("execution_requires_review")


def _response(run, ticket):
    return {"schema_version": 1, WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: ticket}


def grant_automatic_execution(run_id, payload):
    """Read fresh producer history/budget, then serialize a no-effect grant."""
    _body(payload)
    run = ContentFactoryRun.objects.select_related("organization").filter(run_id=run_id).first()
    if run is None:
        raise ContentFactoryRun.DoesNotExist
    require_admission_identity(run, payload)
    require_event_execution(run, payload)
    # Exact retries need no HTTP. Current rights and a local stop still win.
    prior = (run.result or {}).get(CONTROL_KEY, {}).get("grants", {}).get(payload["control_key"])
    if prior is not None:
        with transaction.atomic():
            from organizations.models import Organization
            Organization.objects.select_for_update().get(pk=run.organization_id)
            run = ContentFactoryRun.objects.select_for_update(of=("self",)).select_related("organization").get(pk=run.pk)
            _require_active(run)
            require_current_principal(run)
            require_admission_identity(run, payload)
            require_event_execution(run, payload)
            ticket = append_execution_ticket(run, control_key=payload["control_key"], reason=payload["reason"],
                expected_execution_id=payload[WIRE_EXECUTION_KEY]["execution_id"])
            return _response(run, ticket)
    require_event_execution(run, payload, current=True)
    _require_active(run)
    require_current_principal(run)
    budget = _budget(run_id)
    from .run_reconciliation import reconcile_authoritative_run
    reconciliation = reconcile_authoritative_run(run_id, refresh=True)
    if (reconciliation.get("outcome") != "history_received" or reconciliation.get("has_more")
            or reconciliation.get("active_claim")):
        raise ExecutionControlDenied("execution_history_pending", pending=True)
    # Repository proof/HTTP remains outside the final locks. authority_guard
    # performs its provider preflight before entering its own transaction.
    from contextlib import nullcontext
    from .website_connections import authority_guard
    current = current_execution_request(run)
    guard = authority_guard({**current, "run_id": run.run_id}, action="setup", require_current_head=True) if run.github_repo else nullcontext()
    with guard:
        with transaction.atomic():
            # Keep membership lock order aligned with the original controllers.
            from organizations.models import Organization
            Organization.objects.select_for_update().get(pk=run.organization_id)
            run = ContentFactoryRun.objects.select_for_update(of=("self",)).select_related("organization").get(pk=run.pk)
            require_admission_identity(run, payload)
            require_event_execution(run, payload, current=True)
            _require_active(run)
            require_current_principal(run)
            cursor = validate_budget_observation(run, budget)
            from .run_ownership import RECONCILIATION_RESULT_KEY
            meta = (run.result or {}).get(RECONCILIATION_RESULT_KEY, {})
            if (type(meta.get("cursor")) is not int or cursor != meta["cursor"]
                    or meta.get("has_more") is True or meta.get("claim") is not None):
                raise ExecutionControlDenied("execution_history_pending", pending=True)
            from .run_ownership import EVENT_RESULT_KEY
            projection = (run.result or {}).get(EVENT_RESULT_KEY, {}).get("projection", {})
            if (projection.get("execution_id") != current_execution_ticket(run)["execution_id"]
                    or projection.get("status") not in {"queued", "running"}):
                raise ExecutionControlDenied("execution_history_pending", pending=True)
            record_budget_observation(run, budget)
            ticket = append_execution_ticket(run, control_key=payload["control_key"], reason=payload["reason"],
                expected_execution_id=payload[WIRE_EXECUTION_KEY]["execution_id"])
            return _response(run, ticket)


class RunExecutionTicketView(APIView):
    """Grant no user action, payment or publication authority to a worker."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id):
        """Issue only a fresh, budgeted continuation of the existing admission."""
        from .website_contract import WebsiteAuthorityError
        try:
            return Response(grant_automatic_execution(run_id, request.data))
        except ContentFactoryRun.DoesNotExist:
            return Response({"reason": "not_found"}, status=404)
        except ExecutionControlDenied as exc:
            return Response({"reason": exc.reason, "retryable": exc.pending}, status=409)
        except (RunOwnershipConflict, WebsiteAuthorityError):
            return Response({"reason": "execution_control_refused", "retryable": False}, status=409)
        except requests.RequestException:
            return Response({"reason": "execution_history_pending", "retryable": True}, status=503)
