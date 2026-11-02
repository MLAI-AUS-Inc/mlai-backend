"""Backend admission and identity for migration-free Run ownership.

Only already-authorized backend callers may reserve an execution. A worker
echo cannot create admission, replace its immutable scope, or advance its
authorized execution generation. Legacy records are deliberately not enrolled.
"""
from copy import deepcopy
import json
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction

from .website_contract import CONNECTION_FIELDS, connection_contract, evidence_digest, sanitized_evidence
from .website_operations import OPERATION_FIELDS

OWNER_RESULT_KEY = "run_owner_v1"
OWNER_EXECUTION_KEY = "run_owner_execution_v1"
EVENT_RESULT_KEY = "run_owner_events_v1"
DISPATCH_RESULT_KEY = "run_owner_dispatch_v1"
HOSTED_OBSERVATION_KEY = "hosted_verification_observation_v1"
RECONCILIATION_RESULT_KEY = "run_owner_reconciliation_v1"
CONTROL_RESULT_KEY = "run_owner_control_v1"
CHILDREN_RESULT_KEY = "run_owner_children_v1"
ARTICLE_CHARGE_RESULT_KEY = "run_owner_article_charge_v1"
ARTICLE_PUBLISH_APPROVAL_RESULT_KEY = "run_owner_article_publish_approval_v1"
ARTICLE_PUBLISH_CONTROL_RESULT_KEY = "run_owner_article_publish_control_v1"
VERIFIED_DELIVERY_RESULT_KEY = "run_owner_verified_delivery_v1"
USER_STOP_RESULT_KEY = "run_owner_user_stop_v1"
REVIEW_SOURCE_RESULT_KEY = "review_source_v1"
HOSTED_PUBLICATION_OBSERVATION_KEY = "hosted_publication_observation_v1"
WIRE_REQUEST_KEY = "backend_run_admission_v1"
WIRE_EXECUTION_KEY = "backend_run_execution_v1"
BUDGET_KEY = "backend_run_budget_v1"
R3_BACKEND_RESULT_KEYS = frozenset({OWNER_RESULT_KEY, OWNER_EXECUTION_KEY, EVENT_RESULT_KEY,
                                  DISPATCH_RESULT_KEY, HOSTED_OBSERVATION_KEY,
                                  RECONCILIATION_RESULT_KEY, CONTROL_RESULT_KEY, CHILDREN_RESULT_KEY,
                                  ARTICLE_CHARGE_RESULT_KEY, ARTICLE_PUBLISH_APPROVAL_RESULT_KEY, ARTICLE_PUBLISH_CONTROL_RESULT_KEY,
                                  VERIFIED_DELIVERY_RESULT_KEY, USER_STOP_RESULT_KEY, REVIEW_SOURCE_RESULT_KEY,
                                  HOSTED_PUBLICATION_OBSERVATION_KEY})
R3_BACKEND_REQUEST_KEYS = frozenset({WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY})
SCOPE_FIELDS = (*CONNECTION_FIELDS, *OPERATION_FIELDS, "expected_source_sha", "source_run_id")
SCHEDULER_SERVICES = frozenset({"island_refresh", "daily_discovery", "research_automation", "hosted_auto_register"})
WORKER_WORKFLOW_ALIASES = {
    "article_generation": frozenset({"direct_generate", "confirmed_topic"}),
    "scaffold_articles": frozenset({"article_system_setup"}),
    "scan": frozenset({"repo_scan"}),
}


class RunOwnershipConflict(ValueError):
    """The proposed execution does not match its backend admission."""


def admission_enabled():
    """Whether new executions may enter the Run-first admission protocol."""
    return getattr(settings, "ARTICLES_RUN_OWNERSHIP_ENABLED", False) is True


def is_hosted_proxy_request(request):
    """Recognize the saved server-owned proxy recipe, never an arbitrary setup."""
    if not isinstance(request, dict):
        return False
    try:
        site = UUID(request.get("hosted_site_id"))
        worker = urlsplit(request.get("worker_url") or "")
        port = worker.port
    except (ValueError, TypeError, AttributeError):
        return False
    generation = request.get("connection_generation")
    return (type(generation) is int and generation > 0 and request.get("hosted_site_id") == str(site)
            and request.get("run_id") == f"hosted-proxy-{site.hex}-{generation}"
            and request.get("merge_policy") == "backend_worker"
            and worker.scheme == "https" and bool(worker.hostname)
            and worker.hostname.endswith(".workers.dev") and not worker.username and not worker.password
            and port in (None, 443) and worker.path in {"", "/"} and not worker.query and not worker.fragment)


def worker_workflow_allowed(admitted, actual, *, request=None):
    """Check only verified backend-to-worker aliases; never rewrite admission."""
    return type(admitted) is str and type(actual) is str and (
        actual == admitted or actual in WORKER_WORKFLOW_ALIASES.get(admitted, ())
        or admitted == "article_system_setup" and actual == "hosted_native_verification"
        and is_hosted_proxy_request(request))


def public_run_value(value):
    """Remove internal admission/ticket/receipt namespaces before public output."""
    if isinstance(value, dict):
        return {key: public_run_value(item) for key, item in value.items()
                if key not in R3_BACKEND_RESULT_KEYS | R3_BACKEND_REQUEST_KEYS}
    if isinstance(value, list):
        return [public_run_value(item) for item in value]
    return value


def owner_receipt(run):
    """Return the backend-only admission receipt, or None for a legacy Run."""
    result = getattr(run, "result", None)
    receipt = result.get(OWNER_RESULT_KEY) if isinstance(result, dict) else None
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1
            or not isinstance(receipt.get("identity"), dict)
            or not isinstance(receipt.get("authority"), dict)):
        return None
    return deepcopy(receipt)


def wire_identity(receipt):
    """Return the exact internal identity the admitted worker must echo."""
    return {**deepcopy(receipt["identity"]), "event_protocol": receipt["authority"]["event_protocol"]}


def owns_backend_facts(run):
    """An admitted fact owner stays authoritative independently of rollout flags."""
    receipt = owner_receipt(run)
    return receipt is not None and receipt["authority"].get("backend_facts") is True


def user_stop_intent(run):
    """A current explicit user denial revokes consent without inventing CF state."""
    receipt = owner_receipt(run)
    result = getattr(run, "result", None)
    if receipt is None or receipt["authority"].get("event_protocol") != 1 or not isinstance(result, dict):
        return None
    if USER_STOP_RESULT_KEY not in result:
        return None
    state = result[USER_STOP_RESULT_KEY]
    if (not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or state.get("admission_digest") != evidence_digest(wire_identity(receipt))
            or not isinstance(state.get("commands"), dict) or len(state["commands"]) > 1024):
        return {"invalid": True}
    try:
        current = current_execution_ticket(run)
        for command in state["commands"].values():
            if (not isinstance(command, dict) or command.get("action") != "deny"
                    or type(command.get("user_id")) is not str or not command["user_id"].isdigit()
                    or not isinstance(command.get("requested_at"), str)):
                return {"invalid": True}
            if (command.get("execution_id") == current["execution_id"]
                    and command.get("execution_sha256") == evidence_digest(current)):
                return deepcopy(command)
    except (ValueError, TypeError, KeyError):
        return {"invalid": True}
    return None


def authorized_event_generation(run):
    """The backend-authorized generation; worker observations cannot raise it."""
    return current_execution_ticket(run)["generation"]


def current_execution_ticket(run):
    """Return the exact current backend ticket, never a worker-derived scope."""
    result = run.result if isinstance(run.result, dict) else {}
    execution = result.get(OWNER_EXECUTION_KEY)
    tickets = execution.get("tickets") if isinstance(execution, dict) else None
    ticket = tickets.get(execution.get("current_execution_id")) if isinstance(tickets, dict) else None
    if (owner_receipt(run) is None or not isinstance(ticket, dict)
            or type(ticket.get("generation")) is not int or ticket["generation"] < 0):
        raise RunOwnershipConflict("This execution has no authorized generation.")
    return deepcopy(ticket)


def require_event_execution(run, payload, *, generation=None, current=False):
    """Check an exact retained ticket; old tickets are history, not current work."""
    result = run.result if isinstance(run.result, dict) else {}
    execution = result.get(OWNER_EXECUTION_KEY)
    wire = payload.get(WIRE_EXECUTION_KEY) if isinstance(payload, dict) else None
    identifier = wire.get("execution_id") if isinstance(wire, dict) else None
    tickets = execution.get("tickets") if isinstance(execution, dict) else None
    ticket = tickets.get(identifier) if isinstance(tickets, dict) and isinstance(identifier, str) else None
    receipt = owner_receipt(run)
    if (receipt is None or not isinstance(ticket, dict) or not _exact(wire, ticket)
            or ticket.get("admission_digest") != evidence_digest(wire_identity(receipt))
            or (generation is not None and (type(generation) is not int or generation != ticket.get("generation")))
            or (current and execution.get("current_execution_id") != identifier)):
        raise RunOwnershipConflict("The observation does not carry an authorized execution ticket.")
    return deepcopy(ticket)


def require_admission_identity(run, payload):
    """Validate an exact internal echo without granting lifecycle authority."""
    receipt = owner_receipt(run)
    if (receipt is None or not isinstance(payload, dict)
            or not _exact(payload.get(WIRE_REQUEST_KEY), wire_identity(receipt))):
        raise RunOwnershipConflict("The observation does not match its admitted execution.")
    identity = receipt["identity"]
    if (str(run.organization_id) != identity.get("organization_id")
            or run.workflow != identity.get("workflow") or run.domain != identity.get("domain")
            or run.github_repo != identity.get("github_repo")):
        raise RunOwnershipConflict("The admitted execution identity changed.")
    for field, expected in (("client_request_id", identity["dispatch_key"]),
                            ("domain", identity["domain"]), ("workflow", identity["workflow"])):
        if field in payload and payload[field] != expected:
            raise RunOwnershipConflict("The event envelope changed its admitted identity.")
    if "worker_workflow" in payload and not worker_workflow_allowed(identity["workflow"], payload["worker_workflow"], request=run.run_request):
        raise RunOwnershipConflict("The worker workflow does not match its admitted operation.")
    return receipt


def require_event_identity(run, payload):
    """Validate an exact authoritative-event echo against existing admission."""
    receipt = require_admission_identity(run, payload)
    if receipt["authority"].get("event_protocol") != 1:
        raise RunOwnershipConflict("This admission does not own authoritative events.")
    return receipt


def prepare_owned_callback(payload, remote_run_id):
    """Validate before alias adoption; never materialize an unknown admission.

    Returns None for an unmarked legacy execution. Owned observations require
    the full immutable echo and an already backend-authorized generation.
    The caller still applies its original current-rights and side-effect gates.
    """
    from workflow_runs.models import ContentFactoryRun
    from .run_state import execution_version
    request = payload.get("run_request") if isinstance(payload.get("run_request"), dict) else {}
    wire = payload.get(WIRE_REQUEST_KEY, request.get(WIRE_REQUEST_KEY))
    execution_wire = payload.get(WIRE_EXECUTION_KEY, request.get(WIRE_EXECUTION_KEY))
    key = payload.get("client_request_id") or request.get("client_request_id")
    if not key and isinstance(wire, dict):
        key = wire.get("dispatch_key")
    with transaction.atomic():
        run = admitted_run_for_key(key, lock=True)
        if run is None:
            existing = ContentFactoryRun.objects.select_for_update().filter(run_id=remote_run_id).first()
            if owner_receipt(existing) is not None:
                run = existing
            elif wire is not None:
                raise RunOwnershipConflict("The original backend admission is not available.")
            else:
                return None
        receipt = owner_receipt(run)
        observed_workflow = payload.get("workflow")
        if observed_workflow is not None and not worker_workflow_allowed(receipt["identity"]["workflow"], observed_workflow, request=run.run_request):
            raise RunOwnershipConflict("The worker workflow does not match the admitted operation.")
        envelope = {**payload, WIRE_REQUEST_KEY: wire, "workflow": receipt["identity"]["workflow"]}
        require_admission_identity(run, envelope)
        try:
            version = execution_version(payload)
        except ValueError as exc:
            raise RunOwnershipConflict("A complete typed execution version is required.") from exc
        if version is None or version[0] != authorized_event_generation(run):
            raise RunOwnershipConflict("The worker generation has not been authorized by the backend.")
        require_event_execution(run, {WIRE_EXECUTION_KEY: execution_wire}, generation=version[0], current=True)
        bound = bind_admitted_run(run, remote_run_id)
        # Snapshot/callback normalization is limited to the verified alias. Its
        # immutable backend admission and source request are never rewritten.
        payload["workflow"] = receipt["identity"]["workflow"]
        return bound


def _exact(left, right):
    # JSON protocol identity is type-exact: True must never equal integer 1.
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def user_principal(user_id, actor_id):
    """Build an explicit authenticated user principal without owner substitution."""
    if isinstance(user_id, bool) or not str(user_id or "").isdigit() or int(user_id) <= 0:
        raise RunOwnershipConflict("A verified requesting user is required.")
    if not isinstance(actor_id, str) or len(actor_id) > 100:
        raise RunOwnershipConflict("A bounded backend actor identity is required.")
    return {"kind": "user", "user_id": str(int(user_id)), "actor_id": actor_id}


def scheduler_principal(service):
    """Build a declared backend scheduler principal, never an invented founder."""
    if service not in SCHEDULER_SERVICES:
        raise RunOwnershipConflict("An admitted scheduler service is required.")
    return {"kind": "scheduler", "service": service}


def _identity(*, organization, principal, workflow, payload):
    key = payload.get("client_request_id")
    if (type(key) is not str or not key or key != key.strip() or len(key) > 255
            or type(workflow) is not str or not workflow or len(workflow) > 50
            or not getattr(organization, "pk", None) or payload.get("domain") != organization.domain):
        raise RunOwnershipConflict("An exact admitted startup, workflow and dispatch key are required.")
    if principal.get("kind") == "user":
        principal = user_principal(principal.get("user_id"), principal.get("actor_id"))
    elif principal.get("kind") == "scheduler":
        principal = scheduler_principal(principal.get("service"))
    else:
        raise RunOwnershipConflict("An admitted principal is required.")
    request = sanitized_evidence({key: value for key, value in payload.items()
                                  if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}})
    connection = connection_contract(request)
    scope = {field: deepcopy(connection.get(field, request.get(field))) for field in SCOPE_FIELDS}
    scope = {field: None if type(value) is str and value == "" else value for field, value in scope.items()}
    return {"schema_version": 1, "dispatch_key": key, "organization_id": str(organization.pk),
            "principal": principal, "workflow": workflow, "domain": organization.domain,
            "github_repo": request.get("github_repo") or "", "request_digest": evidence_digest(request),
            "scope": scope}, request


def admitted_run_for_key(key, *, lock=False):
    """Resolve only a pre-existing admitted Run by its original backend key."""
    from workflow_runs.models import ContentFactoryRun
    if type(key) is not str or not key or len(key) > 255:
        return None
    runs = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
    return runs.filter(result__run_owner_v1__identity__dispatch_key=key).first()


def reservation_run_id(dispatch_key):
    """Keep a long logical key traceable within the existing Run ID column."""
    return dispatch_key if len(dispatch_key) <= 100 else "r3-key:" + evidence_digest({"dispatch_key": dispatch_key})


def _admission_budget(payload, *, workflow, existing=None):
    """Freeze a backend cap once; worker/caller values cannot grant a budget."""
    request = deepcopy(payload)
    if existing is None:
        if BUDGET_KEY in request:
            raise RunOwnershipConflict("An external request cannot grant an execution budget.")
        from .run_execution_control import initial_control_budget
        budget = initial_control_budget(workflow)
    else:
        saved = existing.run_request if isinstance(existing.run_request, dict) else {}
        budget = saved.get(BUDGET_KEY)
        if BUDGET_KEY in request and (BUDGET_KEY not in saved or not _exact(request[BUDGET_KEY], budget)):
            raise RunOwnershipConflict("The original execution budget cannot change.")
    if budget is not None:
        request[BUDGET_KEY] = deepcopy(budget)
    return request


def reserve_admitted_run(*, organization, principal, workflow, payload, reserved_run_id=None):
    """Reserve before dispatch, after the caller's existing permission checks.

    No old Run is automatically enrolled and no network call occurs here.
    The returned identity is added to the outbound request only after commit.
    Reservation-only admission does not transfer Job or snapshot ownership.
    """
    admitting = admission_enabled()
    if not admitting and WIRE_REQUEST_KEY not in payload:
        return None
    from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus
    key = payload.get("client_request_id")
    if type(key) is not str or not key or key != key.strip() or len(key) > 255:
        raise RunOwnershipConflict("An exact bounded dispatch key is required.")
    initial_id = reserved_run_id or reservation_run_id(key)
    if (type(initial_id) is not str or not initial_id or len(initial_id) > 100
            or initial_id != initial_id.strip()):
        raise RunOwnershipConflict("A bounded backend reservation identity is required.")
    facts = getattr(settings, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED", False) is True
    events = getattr(settings, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED", False) is True
    with transaction.atomic():
        # Serialize retries even after the initial key has been renamed to its
        # worker ID; run_id uniqueness alone no longer guards that lookup.
        type(organization).objects.select_for_update().get(pk=organization.pk)
        run = admitted_run_for_key(key, lock=True)
        # Resolve an exact retry before consulting mutable backend budget
        # settings. The original cap remains part of the immutable digest.
        prepared = _admission_budget(payload, workflow=workflow, existing=run)
        identity, request = _identity(organization=organization, principal=principal, workflow=workflow, payload=prepared)
        if run is None:
            if not admitting:
                raise RunOwnershipConflict("New Run admission is disabled.")
            if events and not facts:
                raise RunOwnershipConflict("Event authority requires the complete backend fact owner.")
            receipt = {"schema_version": 1, "identity": identity,
                       "authority": {"backend_facts": facts, "event_protocol": 1 if events else None}}
            if initial_id != key or reserved_run_id is not None:
                receipt.update(reservation_run_id=initial_id, fixed_run_id=reserved_run_id is not None)
            wire = wire_identity(receipt)
            ticket = {"schema_version": 1, "execution_id": "admit:" + evidence_digest(wire), "generation": 0,
                      "admission_digest": evidence_digest(wire), "request_digest": identity["request_digest"],
                      "scope": deepcopy(identity["scope"])}
            run, created = ContentFactoryRun.objects.select_for_update().get_or_create(
                run_id=initial_id, defaults={"organization": organization,
                    "workflow": workflow, "domain": organization.domain, "github_repo": identity["github_repo"],
                    "slack_user_id": principal.get("actor_id", ""), "status": ContentFactoryRunStatus.QUEUED,
                    "current_step": "queued", "run_request": {**request, WIRE_REQUEST_KEY: wire, WIRE_EXECUTION_KEY: ticket},
                    "result": {OWNER_RESULT_KEY: receipt, OWNER_EXECUTION_KEY: {
                        "current_execution_id": ticket["execution_id"], "tickets": {ticket["execution_id"]: ticket}}}})
            if not created and owner_receipt(run) is None:
                raise RunOwnershipConflict("An existing legacy execution already owns that key.")
        saved = owner_receipt(run)
        if (saved is None or not _exact(saved["identity"], identity) or run.organization_id != organization.pk
                or run.workflow != workflow or run.domain != organization.domain
                or run.github_repo != identity["github_repo"]
                or (reserved_run_id is not None and saved.get("reservation_run_id") != reserved_run_id)):
            raise RunOwnershipConflict("The dispatch key already owns different admitted inputs.")
        wire = wire_identity(saved)
        ticket = current_execution_ticket(run)
    payload[WIRE_REQUEST_KEY] = wire
    payload[WIRE_EXECUTION_KEY] = ticket
    if BUDGET_KEY in request:
        payload[BUDGET_KEY] = deepcopy(request[BUDGET_KEY])
    return run


def seal_new_run_admission(run, *, created, principal):
    """Seal only a caller's newly created named Run before its first dispatch.

    ``created`` must be the trusted ORM creation result, never request data.
    The caller owns the creation transaction and must finish any original
    charge carry before this call. Existing legacy rows are never enrolled.
    """
    if not admission_enabled():
        return run
    if created is not True or not transaction.get_connection().in_atomic_block:
        raise RunOwnershipConflict("New named admission requires its actual creation transaction.")
    from workflow_runs.models import ContentFactoryRun
    from .models import ContentFactoryJob
    from organizations.models import Organization
    Organization.objects.select_for_update().get(pk=run.organization_id)
    locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
    result = locked.result if isinstance(locked.result, dict) else {}
    request = deepcopy(locked.run_request) if isinstance(locked.run_request, dict) else {}
    progress_fields = ("progress_message_ts", "slack_channel_id", "slack_thread_ts", "slack_root_message_ts",
                       "posted_progress_ids", "last_progress_milestone_key", "last_progress_milestone_index",
                       "last_progress_updated_at", "still_working_pinged_at")
    if (owner_receipt(locked) is not None or locked.status != "queued"
            or locked.current_step not in {"", "queued"} or locked.last_event_emitted_at is not None
            or locked.reconciled_at is not None or locked.artifact_root or locked.resume_available
            or locked.acceptance_summary or locked.verification_summary
            or any(getattr(locked, field) for field in progress_fields)
            or any(key in result for key in ("generation", "state_version", "run_events_v1", "delivery_v1", EVENT_RESULT_KEY))
            or any(key in request for key in R3_BACKEND_REQUEST_KEYS)
            or locked.steps.exists() or ContentFactoryJob.objects.filter(job_id=locked.run_id).exists()):
        raise RunOwnershipConflict("A previously active or enrolled Run cannot become a new admission.")
    request.setdefault("client_request_id", locked.run_id)
    request = _admission_budget(request, workflow=locked.workflow)
    identity, request = _identity(organization=locked.organization, principal=principal,
                                  workflow=locked.workflow, payload=request)
    if identity["github_repo"] != locked.github_repo:
        raise RunOwnershipConflict("The named execution has different admitted repository inputs.")
    if admitted_run_for_key(identity["dispatch_key"], lock=True) is not None:
        raise RunOwnershipConflict("The original dispatch key already owns an admission.")
    facts = getattr(settings, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED", False) is True
    events = getattr(settings, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED", False) is True
    if events and not facts:
        raise RunOwnershipConflict("Event authority requires the complete backend fact owner.")
    receipt = {"schema_version": 1, "identity": identity, "reservation_run_id": locked.run_id, "fixed_run_id": True,
               "authority": {"backend_facts": facts, "event_protocol": 1 if events else None}}
    wire = wire_identity(receipt)
    ticket = {"schema_version": 1, "execution_id": "admit:" + evidence_digest(wire), "generation": 0,
              "admission_digest": evidence_digest(wire), "request_digest": identity["request_digest"],
              "scope": deepcopy(identity["scope"])}
    request.update({WIRE_REQUEST_KEY: wire, WIRE_EXECUTION_KEY: ticket})
    result = {**result, OWNER_RESULT_KEY: receipt, OWNER_EXECUTION_KEY: {
        "current_execution_id": ticket["execution_id"], "tickets": {ticket["execution_id"]: ticket}}}
    ContentFactoryRun.objects.filter(pk=locked.pk).update(run_request=request, result=result)
    run.run_request, run.result = request, result
    return run


def release_dispatch_transport_hold(run):
    """Release only a proven dispatch transport hold on an already locked Run.

    The internal caller must validate the trusted dispatch response or exact
    callback admission/ticket before binding. Real execution stops, request
    inputs, receipts and every original Run clock remain unchanged.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RunOwnershipConflict("A dispatch hold requires the canonical Run transaction.")
    receipt = owner_receipt(run)
    result = run.result if isinstance(run.result, dict) else {}
    hold = result.get(DISPATCH_RESULT_KEY)
    protocol = receipt["authority"].get("event_protocol") if receipt else None
    if (type(protocol) is not int or protocol != 1 or not isinstance(hold, dict)
            or hold.get("state") != "pending_resolution" or run.status != "blocked"
            or run.current_step != "dispatch_pending_resolution"):
        return run
    from workflow_runs.models import ContentFactoryRun
    changed = ContentFactoryRun.objects.filter(pk=run.pk, status="blocked",
        current_step="dispatch_pending_resolution").update(status="queued", current_step="queued")
    if changed:
        run.status, run.current_step = "queued", "queued"
    return run


def bind_admitted_run(run, remote_run_id):
    """Attach an accepted worker ID without deleting history or changing clocks.

    The caller must validate its authenticated response or callback envelope
    and execution version first. A collision is never merged or deleted.
    """
    from workflow_runs.models import ContentFactoryRun
    if type(remote_run_id) is not str or not remote_run_id or remote_run_id != remote_run_id.strip() or len(remote_run_id) > 100:
        raise RunOwnershipConflict("A bounded worker execution identity is required.")
    with transaction.atomic():
        locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        receipt = owner_receipt(locked)
        if receipt is None:
            raise RunOwnershipConflict("Only a backend admission may bind a worker identity.")
        if locked.run_id == remote_run_id:
            return release_dispatch_transport_hold(locked)
        if receipt.get("fixed_run_id") or locked.run_id != receipt.get("reservation_run_id", receipt["identity"]["dispatch_key"]):
            raise RunOwnershipConflict("The admitted execution already has another worker identity.")
        if ContentFactoryRun.objects.filter(run_id=remote_run_id).exclude(pk=locked.pk).exists():
            raise RunOwnershipConflict("The worker identity is already occupied; admission is retained.")
        from .models import ContentFactoryJob
        jobs = ContentFactoryJob.objects.select_for_update()
        carrier = jobs.filter(job_id=locked.run_id).first()
        if carrier is not None and jobs.filter(job_id=remote_run_id).exclude(pk=carrier.pk).exists():
            raise RunOwnershipConflict("The worker identity already has another legacy carrier.")
        original_id = locked.run_id
        try:
            with transaction.atomic():
                ContentFactoryRun.objects.filter(pk=locked.pk).update(run_id=remote_run_id)
        except IntegrityError as exc:
            raise RunOwnershipConflict("The worker identity was concurrently occupied; admission is retained.") from exc
        locked.run_id = remote_run_id
        if carrier is not None:
            # Preserve billing/source identity and the exact Job clocks. This
            # is a routing rename, not a new charge or a full Job fact mirror.
            from .dispatch_binding import _rebind_billing_job
            _rebind_billing_job(original_id, remote_run_id)
            if not ContentFactoryJob.objects.filter(pk=carrier.pk, job_id=remote_run_id).exists():
                raise RunOwnershipConflict("The admitted legacy carrier could not be projected atomically.")
        return release_dispatch_transport_hold(locked)
