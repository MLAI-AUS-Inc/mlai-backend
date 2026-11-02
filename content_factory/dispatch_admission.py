"""Run-first glue for already-authorized direct Content Factory dispatches."""
from copy import deepcopy
import json
from uuid import uuid4

from .run_ownership import (
    DISPATCH_RESULT_KEY, OWNER_RESULT_KEY, WIRE_REQUEST_KEY, RunOwnershipConflict, admission_enabled,
    bind_admitted_run, owner_receipt, reserve_admitted_run, scheduler_principal, user_principal,
)

def reserve_user_dispatch(*, payload, workflow, domain, actor_id, user=None):
    """Reserve using the actual verified actor after the caller's admission gates."""
    if not admission_enabled() and WIRE_REQUEST_KEY not in payload:
        return None
    from organizations.models import Organization
    from integrations.services.github_installations import resolve_user_for_actor_id
    from integrations.services.article_generation import _validate_authenticated_content_factory_actor
    organization = Organization.objects.get(domain=domain)
    user = user or resolve_user_for_actor_id(actor_id)
    if user is None or not user.is_active:
        raise RunOwnershipConflict("The requesting account is unavailable.")
    _validate_authenticated_content_factory_actor(user=user, actor_id=actor_id)
    payload.setdefault("client_request_id", "r3:" + uuid4().hex)
    return reserve_admitted_run(organization=organization, principal=user_principal(user.pk, actor_id),
        workflow=workflow, payload=payload)


def reserve_scheduler_dispatch(*, payload, workflow, organization, service):
    """Reserve a declared scheduler execution without substituting a founder."""
    if not admission_enabled() and WIRE_REQUEST_KEY not in payload:
        return None
    return reserve_admitted_run(organization=organization, principal=scheduler_principal(service),
        workflow=workflow, payload=payload)


class PendingAdmissionResponse:
    """A truthful local holding response, keyed by the original reservation."""

    status_code = 202

    def __init__(self, run):
        self._body = {"run_id": run.run_id, "job_id": run.run_id, "status": "blocked",
                      "current_step": "dispatch_pending_resolution", "dispatch_pending_resolution": True,
                      "retryable": True, "local_dispatch_key": owner_receipt(run)["identity"]["dispatch_key"]}
        self.text = json.dumps(self._body)
        self.content = self.text.encode()

    def json(self):
        """Return a copy so a legacy adapter cannot mutate the stored response."""
        return deepcopy(self._body)

    def raise_for_status(self):
        """The holding acknowledgement is accepted, without claiming enqueue."""
        return None


def pending_response(run, *, key_outcome="unknown", deferred_refund=None):
    """Persist uncertainty without releasing the original admission or spend."""
    from django.db import transaction
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = dict(current.result or {})
        previous = result.get(DISPATCH_RESULT_KEY) if isinstance(result.get(DISPATCH_RESULT_KEY), dict) else {}
        result[DISPATCH_RESULT_KEY] = {**previous, "state": "pending_resolution", "key_outcome": key_outcome,
            "client_request_id": owner_receipt(current)["identity"]["dispatch_key"]}
        if deferred_refund:
            result[DISPATCH_RESULT_KEY]["deferred_refund"] = deepcopy(deferred_refund)
        fields = {"result": result}
        # A delayed transport response cannot demote callback-first execution.
        receipt = result[OWNER_RESULT_KEY]
        if current.run_id == receipt.get("reservation_run_id", receipt["identity"]["dispatch_key"]) and current.status == "queued":
            fields.update(status="blocked", current_step="dispatch_pending_resolution")
        ContentFactoryRun.objects.filter(pk=current.pk).update(**fields)
        current.refresh_from_db()
    return PendingAdmissionResponse(current)


def accept_dispatch_response(run, response):
    """Bind a trusted response, or retain uncertainty under the original key."""
    if run is None:
        return response
    try:
        body = response.json()
    except (ValueError, AttributeError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if body.get("client_request_id") not in (None, "", owner_receipt(run)["identity"]["dispatch_key"]):
        return pending_response(run)
    remote_id = body.get("run_id") or body.get("job_id") or body.get("task_id")
    if response.status_code in {200, 201, 202} and remote_id:
        bind_admitted_run(run, remote_id)
        return response
    if body.get("code") == "dispatch_rejected" and response.status_code in {400, 401, 403, 409, 410, 422}:
        return response
    return resolve_dispatch_outcome(run, fallback=response)


def post_dispatch(run, post, *args, **kwargs):
    """Keep original identity after uncertain direct HTTP, with legacy calls intact."""
    from integrations.http_client import RequestException
    try:
        response = post(*args, **kwargs)
    except RequestException:
        if run is None:
            raise
        return resolve_dispatch_outcome(run)
    return accept_dispatch_response(run, response)


def resolve_dispatch_outcome(run, *, fallback=None):
    """Adopt only an exact original-key lookup; unknown keeps the same slot."""
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key
    receipt = owner_receipt(run)
    key = receipt["identity"]["dispatch_key"]
    known, body = _lookup_content_factory_dispatch_by_key(_content_factory_remote_config(), key)
    body = body if isinstance(body, dict) else {}
    declared = body.get("client_request_id")
    if declared not in (None, "", key):
        return pending_response(run)
    if known == "started" and body.get("run_id"):
        bound = bind_admitted_run(run, body["run_id"])
        response = PendingAdmissionResponse(bound)
        response._body = {**body, "run_id": bound.run_id, "job_id": bound.run_id,
                          "dispatch_recovered_by_key": True}
        response.text = json.dumps(response._body)
        response.content = response.text.encode()
        return response
    # Authenticated absence alone needs the existing dispatch grace resolver;
    # it must not turn a just-accepted/uncertain request into an immediate refund.
    return pending_response(run, key_outcome=known)
