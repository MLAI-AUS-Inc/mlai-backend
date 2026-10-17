"""Private native recovery references and provider-write barrier, without Git."""

from copy import deepcopy
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_budget as native_budget
from . import cms_goal_dispatch as dispatch
from . import cms_goal_contract as goals
from . import website_goal_budget as budget
from .cms_goal_recovery_contract import make_locator, validate_locator, validate_receipt, NativeRecoveryContractError
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import validate_failure

PLAN_FIELDS = {"schema_version", "state", "locator", "failure", "started_at", "receipt"}
CAPABILITY_FIELDS = {"schema_version", "kind", "run_id", "generation", "state_version", "locator", "failure", "recovery_receipt"}
ACTIVE = frozenset({"reserved", "submitted"})


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native article recovery changed or cannot proceed.")


def _pending():
    return WebsiteAuthorityError("website_prepare_dependency_transient", "The original native article recovery is waiting for its recorded outcome.", retryable=True)


def enabled(operation):
    """Require native enrollment, B1 policy and independent recovery/budget flags."""
    from .website_failure_scope import taxonomy_enabled
    return bool(getattr(settings, "ARTICLES_CMS_GOAL_RECOVERY_ENABLED", False) and budget.enabled() and
        taxonomy_enabled(operation.connection.organization_id) and authority.enabled(operation.connection.organization_id))


def validate_plan(value, operation):
    """Reject changed private plans before model or provider work is admitted."""
    try:
        if (not isinstance(value, dict) or set(value) != PLAN_FIELDS or type(value["schema_version"]) is not int or
                value["schema_version"] != 1 or value["state"] not in ACTIVE | {"observed"}):
            raise _deny()
        locator = validate_locator(value["locator"])
        failure = validate_failure(value["failure"])
        if (locator["goal_operation_id"] != str(operation.pk) or
                locator["run_id"] != operation.payload["source_run_id"] or
                failure["failure_fingerprint"] != locator["failure_fingerprint"] or
                failure["registry_sha256"] != locator["failure_registry_sha256"] or
                failure["failure_class"] not in {"transient", "repairable"} or failure["action"] is not None):
            raise _deny()
        goals._time(value["started_at"])
        if value["receipt"] is not None: validate_receipt(value["receipt"], locator)
        elif value["state"] != "reserved": raise _deny()
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def locator(operation, run):
    """Use only the actual original mirrored run and registered causal failure."""
    try:
        failure = validate_failure(run.result["failure"])
        if run.status not in {"failed", "blocked"} or failure["failure_class"] not in {"transient", "repairable"} or failure["action"] is not None:
            raise _deny()
        return make_locator(goal_operation_id=str(operation.pk), run_id=run.run_id,
            envelope_digest=run.run_request["publishing_channel_artifact"]["digest"], run_generation=run.result["generation"],
            state_version=run.result["state_version"], failure_occurrence_id=run.result["failure_occurrence_id"],
            failure_fingerprint=failure["failure_fingerprint"], failure_registry_sha256=failure["registry_sha256"])
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def _no_issued_provider_effect(operation, run):
    """Serialize against media/post allowances under the owning organization lock.

    Unknown or confirmed effects keep the old source readable. Neither can be
    repaired by generating a new source, uploading again or falling back.
    """
    from .website_models import WebsiteConnectionOperation
    from . import cms_media_contract as media
    from . import cms_post_contract as posts
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection_id=operation.connection_id,
        generation=operation.generation, action__in={"cms-media", "cms-publish"})
    try:
        for row in rows:
            payload = row.payload
            if row.action == "cms-media":
                intents, states = payload["intents"], payload["states"]
                if not isinstance(intents, list) or not isinstance(states, list) or len(intents) != len(states): raise _deny()
                for intent, state in zip(intents, states):
                    media.validate_intent(intent)
                    media.validate_state(state, intent)
                    if intent["source_run_id"] == run.run_id and state["create_count"] != 0: raise _deny()
            else:
                intent = posts.validate_intent(payload["intent"])
                state = posts.validate_post_state(payload["post_state"], intent)
                if intent["source_run_id"] == run.run_id and state["create_count"] != 0: raise _deny()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def _current(scope, *, user=None, context=None, now=None):
    """A typed current lease is required at each private control boundary."""
    from .website_models import WebsiteConnectionOperation
    from .cms_goal_effects import require_work
    if not isinstance(scope, authority.CmsGoalDispatch): raise _deny()
    operation = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
        pk=scope.operation_id, action=authority.ACTION).first()
    if operation is None or not enabled(operation): raise _deny()
    value = authority.current(operation, user=user, lease_id=scope.lease_id)
    if context is not None and (str(context.company.pk) != value.scope["binding"]["company_id"] or
            str(context.organization.pk) != value.scope["binding"]["organization_id"]): raise _deny()
    row = dispatch.original_dispatch(operation)
    run = dispatch.original_run(operation, row) if row is not None else None
    if run is None or run.run_id == row.client_request_id: raise _deny()
    require_work(row.payload["request"], now=now, action="first-article-generation")
    return operation, value, row, run


def prepare(scope, *, now=None):
    """Reserve a barrier before HTTP; never switch the original source or hold."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    initial, _value, _row, _run = _current(scope, now=now)
    now = now or timezone.now()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=initial.connection.organization_id)
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=initial.pk, action=authority.ACTION).first()
        current, value, _row, run = _current(scope, now=now)
        if operation is None or current.pk != operation.pk: raise _deny()
        previous = operation.receipt.get("native_recovery")
        if previous is not None:
            previous = validate_plan(previous, operation)
            if previous["state"] in ACTIVE: return previous
        observed = locator(operation, run)
        usage = run.result.get("usage")
        if (not budget.valid_usage(usage, run.run_id) or usage["generation"] != observed["run_generation"] or
                usage["state_version"] != observed["state_version"] or
                not all(budget.numeric(usage.get(key)) for key in ("model_usd", "active_seconds"))): raise _deny()
        _no_issued_provider_effect(operation, run)
        selected = native_budget.account_usage(native_budget.validate(operation.receipt.get("native_budget"), value.goal, now=now), usage, run_id=run.run_id)
        if budget.exhaustion(selected, now=now, action="first-article-generation") is not None: raise _deny()
        plan = {"schema_version": 1, "state": "reserved", "locator": observed, "failure": validate_failure(run.result["failure"]),
            "started_at": now.isoformat(), "receipt": None}
        operation.payload = {**deepcopy(operation.payload), "source_run_id": run.run_id}
        operation.receipt = {**deepcopy(operation.receipt), "native_recovery": plan, "native_budget": selected}
        operation.save(update_fields=["payload", "receipt", "updated_at"])
        return deepcopy(plan)


def blocks_provider_work(operation):
    """Keep every original provider allowance fenced until new source is observed."""
    plan = operation.receipt.get("native_recovery")
    if plan is None: return False
    plan = validate_plan(plan, operation)
    if plan["state"] in ACTIVE: return True
    run = dispatch.original_run(operation)
    source = authority.historical(operation).goal["source"]
    return (run is None or source is None or source["run_id"] != run.run_id or
        source["run_generation"] != run.result.get("generation") or
        source["run_generation"] != plan["receipt"]["target_generation"])


def observe_progress(scope, *, now=None):
    """Clear the barrier only after the real original run reports its new source."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    initial, _value, _row, _run = _current(scope, now=now)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=initial.connection.organization_id)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=initial.pk, action=authority.ACTION).first()
        _operation, value, _row, run = _current(scope, now=now)
        plan = validate_plan(saved.receipt.get("native_recovery"), saved)
        if plan["receipt"] is None: return False
        target = plan["receipt"]["target_generation"]
        generation = run.result.get("generation")
        if generation == plan["locator"]["run_generation"]: return False
        if type(generation) is not int or generation != target or run.status in {"cancelled", "denied"}: raise _deny()
        _no_issued_provider_effect(saved, run)
        selected = goals.bind_source(value.goal, {"run_id": run.run_id, "run_generation": generation,
            "envelope_digest": plan["locator"]["envelope_digest"]}, expected_scope=value.scope, now=now)
        saved.receipt = {**deepcopy(saved.receipt), "goal": selected, "native_recovery": {**plan, "state": "observed"}}
        saved.save(update_fields=["receipt", "updated_at"])
        return True


def _authorize_control(scope, *, user=None, context=None, now=None, for_post=False):
    operation, value, _row, run = _current(scope, user=user, context=context, now=now)
    plan = validate_plan(operation.receipt.get("native_recovery"), operation)
    if plan["state"] not in ACTIVE: raise _deny()
    if plan["locator"]["envelope_digest"] != run.run_request["publishing_channel_artifact"]["digest"]: raise _deny()
    # A reply may be lost after the producer advanced. Its memo can acknowledge
    # that exact old locator, but cannot select a later failure or run.
    generation = run.result.get("generation")
    if type(generation) is not int or generation not in {plan["locator"]["run_generation"], plan["locator"]["run_generation"] + 1}: raise _deny()
    if for_post and (generation != plan["locator"]["run_generation"] or locator(operation, run) != plan["locator"]): raise _deny()
    return operation, value, run, plan


def _body(response):
    try:
        if response.status_code in {429, 500, 502, 503, 504}: raise _pending()
        if response.status_code != 200 or len(response.content) > 65536 or not response.headers.get("Content-Type", "").startswith("application/json"):
            raise _deny()
        value = response.json()
        if not isinstance(value, dict): raise _deny()
        return value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _deny() from None


def deliver(scope, *, user=None, context=None, now=None, expected_run_id=None):
    """Negotiate support first, then transmit only the original durable locator."""
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    operation, _value, run, plan = _authorize_control(scope, user=user, context=context, now=now)
    if expected_run_id is not None and expected_run_id != run.run_id: raise _deny()
    remote = _content_factory_remote_config()
    if not remote["enabled"]: raise _pending()
    url = f"{remote['base_url']}/api/runs/{quote(run.run_id, safe='')}/cms-goal-recovery"
    try:
        require_unlocked_remote_call()
        capability = _body(http_client.get(url, params={"locator_digest": plan["locator"]["digest"]},
            headers=_content_factory_headers(), timeout=(3, 15)))
        if (set(capability) != CAPABILITY_FIELDS or type(capability["schema_version"]) is not int or capability["schema_version"] != 1 or
                capability["kind"] != "cms_goal_recovery_capability" or capability["run_id"] != run.run_id or
                any(type(capability[key]) is not int or capability[key] < 0 for key in ("generation", "state_version"))): raise _deny()
        receipt = capability["recovery_receipt"]
        if receipt is None:
            if (validate_locator(capability["locator"]) != plan["locator"] or
                    validate_failure(capability["failure"]) != plan["failure"] or
                    capability["generation"] != plan["locator"]["run_generation"] or
                    capability["state_version"] != plan["locator"]["state_version"]): raise _deny()
            _authorize_control(scope, user=user, context=context, now=now, for_post=True)
            require_unlocked_remote_call()
            receipt = _body(http_client.post(url, json={"locator": plan["locator"]}, headers=_content_factory_headers(), timeout=(3, 15)))
        receipt = validate_receipt(receipt, plan["locator"])
        if capability["recovery_receipt"] is not None and (validate_locator(capability["locator"]) != plan["locator"] or
                capability["generation"] < receipt["target_generation"]): raise _deny()
    except http_client.RequestException:
        # The producer may have committed. The barrier and locator survive;
        # the next lease discovers the same receipt before any retransmission.
        raise _pending() from None
    except (NativeRecoveryContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation.pk, action=authority.ACTION).first()
        _current(scope, user=user, context=context, now=now)
        current = validate_plan(saved.receipt.get("native_recovery"), saved)
        if current["locator"] != plan["locator"] or current["receipt"] is not None and current["receipt"] != receipt: raise _deny()
        saved.receipt = {**deepcopy(saved.receipt), "native_recovery": {**current, "state": "submitted", "receipt": receipt}}
        saved.save(update_fields=["receipt", "updated_at"])
    return {"status": "recovery_reserved", "run_id": run.run_id, "recovery_receipt": receipt, "already_queued": True}


def control(*, scope, user, context, run, action, data):
    """Canonical private control entry; owner JSON cannot create this selector."""
    from rest_framework.response import Response
    if not isinstance(scope, authority.CmsGoalDispatch) or action != "resume" or data: raise _deny()
    _operation, _value, current, _plan = _authorize_control(scope, user=user, context=context)
    if current.pk != run.pk or current.run_id != run.run_id: raise _deny()
    from .vibe_marketing_views import _call_content_factory_run_action
    return Response(_call_content_factory_run_action(run_id=run.run_id, action="resume", payload={},
        workflow=run.workflow, cms_goal_scope=scope), status=202)
