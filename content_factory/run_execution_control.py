"""Backend-issued execution generations with retained, immutable admission.

Tickets grant only a bounded continuation of an existing execution. They do
not approve publication, create a charge or permit changed drafting inputs.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from django.conf import settings
from django.db import transaction

from .run_ownership import (
    OWNER_EXECUTION_KEY, SCOPE_FIELDS, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    RunOwnershipConflict, current_execution_ticket, owner_receipt,
    require_admission_identity, require_event_execution, wire_identity,
)
from .website_contract import evidence_digest

CONTROL_KEY = "run_owner_control_v1"
BUDGET_KEY = "backend_run_budget_v1"
MAX_TICKETS = 1024
AUTOMATIC_REASONS = frozenset({"quality_repair", "orphan_resume"})
CURRENT_CONTEXT_FIELDS = frozenset({"configuration_revision", "github_installation_id"})
_KEY = re.compile(r"[a-f0-9]{64}\Z")


class ExecutionControlDenied(RunOwnershipConflict):
    """A bounded refusal which carries no provider or account information."""

    def __init__(self, reason="execution_control_refused", *, pending=False):
        self.reason, self.pending = reason, pending
        super().__init__(reason)


def _decimal(value, *, positive=False):
    if type(value) is not str or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,12})?", value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and (number > 0 if positive else number >= 0) and number <= 1_000_000 else None


def valid_control_budget(value):
    """Validate the original backend cap; a worker cannot enroll itself."""
    return (isinstance(value, dict)
        and set(value) == {"schema_version", "runtime", "max_model_usd", "max_model_tokens"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and type(value["runtime"]) is str and value["runtime"] in {"maintained_sdk", "article"}
        and _decimal(value["max_model_usd"], positive=True) is not None
        and type(value["max_model_tokens"]) is int and 0 < value["max_model_tokens"] <= 20_000_000)


def initial_control_budget(workflow):
    """Read only backend configuration before admission; zero keeps it off."""
    raw = getattr(settings, "ARTICLES_RUN_CONTROL_MODEL_USD", "0")
    tokens = getattr(settings, "ARTICLES_RUN_CONTROL_MODEL_TOKENS", 0)
    if isinstance(raw, bool) or type(raw) not in {int, float, str}:
        return None
    usd = str(raw)
    setup = workflow in {"article_system_setup", "scaffold_articles"}
    if setup and getattr(settings, "ARTICLES_RUN_SDK_CONTROL_ENABLED", False) is not True:
        return None
    value = {"schema_version": 1, "runtime": "maintained_sdk" if setup else "article",
             "max_model_usd": usd, "max_model_tokens": tokens}
    return value if valid_control_budget(value) else None


def current_execution_request(run):
    """Apply only the backend current ticket's scope to the original inputs."""
    request = deepcopy(run.run_request or {})
    if owner_receipt(run) is not None:
        ticket = current_execution_ticket(run)
        execution = (run.result or {}).get(OWNER_EXECUTION_KEY, {})
        contexts = execution.get("contexts", {})
        context = contexts.get(ticket["execution_id"], {}) if isinstance(contexts, dict) else None
        if not isinstance(context, dict) or not set(context) <= CURRENT_CONTEXT_FIELDS:
            raise ExecutionControlDenied()
        request.update(deepcopy(context))
        for field, value in ticket["scope"].items():
            if value is None:
                request.pop(field, None)
            else:
                request[field] = deepcopy(value)
        request[WIRE_EXECUTION_KEY] = ticket
    return request


def _request_digest(request):
    return evidence_digest({key: value for key, value in request.items()
                            if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}})


def require_current_principal(run):
    """Recheck the actual admitted requester and current startup write rights."""
    from django.contrib.auth import get_user_model
    from founder_tools import startup_team
    from integrations.services.article_generation import ArticleGenerationError, _validate_authenticated_content_factory_actor
    principal = owner_receipt(run)["identity"]["principal"]
    if principal.get("kind") != "user":
        # Scheduler continuation needs a service-specific current permission
        # gate; an invented founder or a billing owner is never a substitute.
        raise ExecutionControlDenied("execution_principal_unavailable")
    user = get_user_model().objects.filter(pk=principal["user_id"], is_active=True).first()
    if user is None:
        raise ExecutionControlDenied("execution_principal_unavailable")
    try:
        _validate_authenticated_content_factory_actor(user=user, actor_id=principal["actor_id"])
    except (ArticleGenerationError, ValueError) as exc:
        raise ExecutionControlDenied("execution_principal_unavailable") from exc
    with startup_team._locked(run.organization) as (_, roster):
        if roster and roster.get("frozen"):
            raise ExecutionControlDenied("execution_permission_changed")
        if startup_team._owner(run.organization) == user.pk:
            return
        if (run.workflow in startup_team.ARTICLE_WORKFLOWS and startup_team.enabled()
                and "write_articles" in startup_team._rights(user, run.organization, roster)):
            return
    raise ExecutionControlDenied("execution_permission_changed")


def append_execution_ticket(run, *, control_key, reason, scope=None, increment_generation=True,
                            expected_execution_id=None, context=None):
    """Append inside a caller's locked, authorized backend transaction.

    ``scope`` is backend-derived proof from the existing source/operation
    controller, never an HTTP patch. Callers retain all original effect gates.
    Same-key retry returns its exact ticket without advancing any generation.
    """
    if (not transaction.get_connection().in_atomic_block or type(control_key) is not str or not _KEY.fullmatch(control_key)
            or type(reason) is not str or reason not in AUTOMATIC_REASONS | {"source_repin", "user_resume", "operation_resume", "connection_rebind", "user_revision"}
            or type(increment_generation) is not bool):
        raise ExecutionControlDenied()
    receipt = owner_receipt(run)
    if receipt is None or receipt["authority"].get("event_protocol") != 1:
        raise ExecutionControlDenied()
    control = deepcopy((run.result or {}).get(CONTROL_KEY, {"schema_version": 1, "grants": {}}))
    if (not isinstance(control, dict) or type(control.get("schema_version")) is not int
            or control["schema_version"] != 1 or not isinstance(control.get("grants"), dict)):
        raise ExecutionControlDenied()
    execution = deepcopy(run.result[OWNER_EXECUTION_KEY])
    old = current_execution_ticket(run)
    contexts = execution.setdefault("contexts", {})
    if not isinstance(contexts, dict):
        raise ExecutionControlDenied()
    new_context = deepcopy(contexts.get(old["execution_id"], {}) if context is None else context)
    if (not isinstance(new_context, dict) or not set(new_context) <= CURRENT_CONTEXT_FIELDS
            or any(type(value) not in {str, int} or isinstance(value, bool) for value in new_context.values())):
        raise ExecutionControlDenied()
    prior = control["grants"].get(control_key)
    if prior is not None:
        command = prior.get("command", {})
        if (command.get("reason") != reason or command.get("increment_generation") is not increment_generation
                or scope is not None and evidence_digest(command.get("scope")) != evidence_digest(scope)
                or context is not None and evidence_digest(command.get("context", {})) != evidence_digest(context)
                or expected_execution_id is not None and expected_execution_id not in {
                    prior.get("previous_execution_id"), prior.get("execution_id")}):
            raise ExecutionControlDenied("execution_control_key_conflict")
        ticket = execution["tickets"].get(prior.get("execution_id"))
        if not isinstance(ticket, dict):
            raise ExecutionControlDenied()
        return deepcopy(ticket)
    if expected_execution_id is not None and expected_execution_id != old["execution_id"]:
        raise ExecutionControlDenied("execution_ticket_changed")
    current_request = current_execution_request(run)
    current_request.update(new_context)
    new_scope = deepcopy(old["scope"] if scope is None else scope)
    if set(new_scope) != set(SCOPE_FIELDS):
        raise ExecutionControlDenied()
    from .run_event_authority import _scope
    if not _scope(new_scope) or old["generation"] + int(increment_generation) >= 2 ** 53:
        raise ExecutionControlDenied()
    for field, value in new_scope.items():
        if value is None:
            current_request.pop(field, None)
        else:
            current_request[field] = deepcopy(value)
    digest = _request_digest(current_request)
    command = {"reason": reason, "scope": new_scope, "request_digest": digest,
               "increment_generation": increment_generation, "context": new_context}
    if type(increment_generation) is not bool or len(execution["tickets"]) >= MAX_TICKETS:
        raise ExecutionControlDenied("execution_control_limit")
    ticket = {"schema_version": 1, "execution_id": "control:" + control_key,
        "generation": old["generation"] + int(increment_generation),
        "admission_digest": evidence_digest(wire_identity(receipt)), "request_digest": digest, "scope": new_scope}
    execution["tickets"][ticket["execution_id"]] = ticket
    contexts[ticket["execution_id"]] = new_context
    execution["current_execution_id"] = ticket["execution_id"]
    # Query projection only; every effect rechecks the immutable current ticket.
    execution["current_scope"] = deepcopy(new_scope)
    control["grants"][control_key] = {"command": command, "previous_execution_id": old["execution_id"],
                                     "execution_id": ticket["execution_id"]}
    from workflow_runs.models import ContentFactoryRun
    result = {**run.result, OWNER_EXECUTION_KEY: execution, CONTROL_KEY: control}
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result
    return deepcopy(ticket)


def validate_budget_observation(run, payload):
    """Check authenticated conservative counters against the original cap."""
    expected = {"schema_version", "run_id", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY,
                "event_cursor", "used_model_usd_upper_bound", "used_model_tokens", "uncertain_calls"}
    budget = (run.run_request or {}).get(BUDGET_KEY)
    receipt = owner_receipt(run)
    if receipt is None or _request_digest(run.run_request or {}) != receipt["identity"]["request_digest"]:
        raise ExecutionControlDenied("execution_budget_unavailable")
    if (not valid_control_budget(budget) or not isinstance(payload, dict) or set(payload) != expected
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("run_id") != run.run_id or not valid_control_budget(payload.get(BUDGET_KEY))
            or evidence_digest(payload.get(BUDGET_KEY)) != evidence_digest(budget)):
        raise ExecutionControlDenied("execution_budget_unavailable")
    require_admission_identity(run, payload)
    require_event_execution(run, payload, current=True)
    usd = _decimal(payload.get("used_model_usd_upper_bound"))
    tokens, uncertain, cursor = (payload.get(key) for key in ("used_model_tokens", "uncertain_calls", "event_cursor"))
    if (usd is None or any(type(value) is not int or not 0 <= value < 2 ** 53 for value in (tokens, uncertain, cursor))):
        raise ExecutionControlDenied("execution_budget_unavailable")
    if usd >= _decimal(budget["max_model_usd"], positive=True) or tokens >= budget["max_model_tokens"]:
        raise ExecutionControlDenied("execution_budget_exhausted")
    prior = (run.result or {}).get(CONTROL_KEY, {}).get("budget_high_water")
    if prior is not None:
        old_usd = _decimal(prior.get("used_model_usd_upper_bound")) if isinstance(prior, dict) else None
        old_tokens = prior.get("used_model_tokens") if isinstance(prior, dict) else None
        if old_usd is None or type(old_tokens) is not int or usd < old_usd or tokens < old_tokens:
            raise ExecutionControlDenied("execution_budget_regressed")
    return cursor


def require_control_budget(run):
    """Refuse unbudgeted original admission before asking the worker anything."""
    receipt = owner_receipt(run)
    if (receipt is None or not valid_control_budget((run.run_request or {}).get(BUDGET_KEY))
            or _request_digest(run.run_request or {}) != receipt["identity"]["request_digest"]):
        raise ExecutionControlDenied("execution_budget_unavailable")


def record_budget_observation(run, payload):
    """Retain conservative peaks in the same transaction as an accepted grant."""
    if not transaction.get_connection().in_atomic_block:
        raise ExecutionControlDenied()
    validate_budget_observation(run, payload)
    result = deepcopy(run.result)
    control = result.setdefault(CONTROL_KEY, {"schema_version": 1, "grants": {}})
    control["budget_high_water"] = {key: deepcopy(payload[key]) for key in
        ("used_model_usd_upper_bound", "used_model_tokens", "uncertain_calls", "event_cursor")}
    from workflow_runs.models import ContentFactoryRun
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result
