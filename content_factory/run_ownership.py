"""Backend admission and identity for migration-free Run ownership.

Only already-authorized backend callers may reserve an execution. A worker
echo cannot create admission, replace its immutable scope, or advance its
authorized execution generation. Legacy records are deliberately not enrolled.
"""
from copy import deepcopy
import json

from django.conf import settings
from django.db import IntegrityError, transaction

from .website_contract import CONNECTION_FIELDS, connection_contract, evidence_digest, sanitized_evidence
from .website_operations import OPERATION_FIELDS

OWNER_RESULT_KEY = "run_owner_v1"
OWNER_EXECUTION_KEY = "run_owner_execution_v1"
EVENT_RESULT_KEY = "run_owner_events_v1"
WIRE_REQUEST_KEY = "backend_run_admission_v1"
SCOPE_FIELDS = (*CONNECTION_FIELDS, *OPERATION_FIELDS, "expected_source_sha", "source_run_id")
SCHEDULER_SERVICES = frozenset({"island_refresh", "daily_discovery", "research_automation"})
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


def worker_workflow_allowed(admitted, actual):
    """Check only verified backend-to-worker aliases; never rewrite admission."""
    return type(admitted) is str and type(actual) is str and (
        actual == admitted or actual in WORKER_WORKFLOW_ALIASES.get(admitted, ()))


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


def authorized_event_generation(run):
    """The backend-authorized generation; worker observations cannot raise it."""
    result = run.result if isinstance(run.result, dict) else {}
    execution = result.get(OWNER_EXECUTION_KEY)
    generation = execution.get("generation") if isinstance(execution, dict) else None
    if owner_receipt(run) is None or type(generation) is not int or generation < 0:
        raise RunOwnershipConflict("This execution has no authorized generation.")
    return generation


def require_event_identity(run, payload):
    """Validate an exact authoritative-event echo against existing admission."""
    receipt = owner_receipt(run)
    if (receipt is None or receipt["authority"].get("event_protocol") != 1
            or not isinstance(payload, dict) or not _exact(payload.get(WIRE_REQUEST_KEY), wire_identity(receipt))):
        raise RunOwnershipConflict("The event does not match an admitted event owner.")
    identity = receipt["identity"]
    if (str(run.organization_id) != identity.get("organization_id")
            or run.workflow != identity.get("workflow") or run.domain != identity.get("domain")
            or run.github_repo != identity.get("github_repo")):
        raise RunOwnershipConflict("The admitted execution identity changed.")
    for field, expected in (("client_request_id", identity["dispatch_key"]),
                            ("domain", identity["domain"]), ("workflow", identity["workflow"])):
        if field in payload and payload[field] != expected:
            raise RunOwnershipConflict("The event envelope changed its admitted identity.")
    if "worker_workflow" in payload and not worker_workflow_allowed(identity["workflow"], payload["worker_workflow"]):
        raise RunOwnershipConflict("The worker workflow does not match its admitted operation.")
    return receipt


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
    if (type(key) is not str or not key or key != key.strip() or len(key) > 100
            or type(workflow) is not str or not workflow or len(workflow) > 50
            or not getattr(organization, "pk", None) or payload.get("domain") != organization.domain):
        raise RunOwnershipConflict("An exact admitted startup, workflow and dispatch key are required.")
    if principal.get("kind") == "user":
        principal = user_principal(principal.get("user_id"), principal.get("actor_id"))
    elif principal.get("kind") == "scheduler":
        principal = scheduler_principal(principal.get("service"))
    else:
        raise RunOwnershipConflict("An admitted principal is required.")
    request = sanitized_evidence({key: value for key, value in payload.items() if key != WIRE_REQUEST_KEY})
    connection = connection_contract(request)
    scope = {field: deepcopy(connection.get(field, request.get(field))) for field in SCOPE_FIELDS}
    return {"schema_version": 1, "dispatch_key": key, "organization_id": str(organization.pk),
            "principal": principal, "workflow": workflow, "domain": organization.domain,
            "github_repo": request.get("github_repo") or "", "request_digest": evidence_digest(request),
            "scope": scope}, request


def admitted_run_for_key(key, *, lock=False):
    """Resolve only a pre-existing admitted Run by its original backend key."""
    from workflow_runs.models import ContentFactoryRun
    if type(key) is not str or not key or len(key) > 100:
        return None
    runs = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
    return runs.filter(result__run_owner_v1__identity__dispatch_key=key).first()


def reserve_admitted_run(*, organization, principal, workflow, payload):
    """Reserve before dispatch, after the caller's existing permission checks.

    No old Run is automatically enrolled and no network call occurs here.
    The returned identity is added to the outbound request only after commit.
    Reservation-only admission does not transfer Job or snapshot ownership.
    """
    admitting = admission_enabled()
    if not admitting and WIRE_REQUEST_KEY not in payload:
        return None
    from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus
    identity, request = _identity(organization=organization, principal=principal, workflow=workflow, payload=payload)
    facts = getattr(settings, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED", False) is True
    events = getattr(settings, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED", False) is True
    receipt = {"schema_version": 1, "identity": identity,
               "authority": {"backend_facts": facts, "event_protocol": 1 if events else None}}
    wire = wire_identity(receipt)
    with transaction.atomic():
        # Serialize retries even after the initial key has been renamed to its
        # worker ID; run_id uniqueness alone no longer guards that lookup.
        type(organization).objects.select_for_update().get(pk=organization.pk)
        run = admitted_run_for_key(identity["dispatch_key"], lock=True)
        if run is None:
            if not admitting:
                raise RunOwnershipConflict("New Run admission is disabled.")
            if events and not facts:
                raise RunOwnershipConflict("Event authority requires the complete backend fact owner.")
            run, created = ContentFactoryRun.objects.select_for_update().get_or_create(
                run_id=identity["dispatch_key"], defaults={"organization": organization,
                    "workflow": workflow, "domain": organization.domain, "github_repo": identity["github_repo"],
                    "slack_user_id": principal.get("actor_id", ""), "status": ContentFactoryRunStatus.QUEUED,
                    "current_step": "queued", "run_request": {**request, WIRE_REQUEST_KEY: wire},
                    "result": {OWNER_RESULT_KEY: receipt, OWNER_EXECUTION_KEY: {"generation": 0}}})
            if not created and owner_receipt(run) is None:
                raise RunOwnershipConflict("An existing legacy execution already owns that key.")
        saved = owner_receipt(run)
        if (saved is None or not _exact(saved["identity"], identity) or run.organization_id != organization.pk
                or run.workflow != workflow or run.domain != organization.domain
                or run.github_repo != identity["github_repo"]):
            raise RunOwnershipConflict("The dispatch key already owns different admitted inputs.")
        wire = wire_identity(saved)
    payload[WIRE_REQUEST_KEY] = wire
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
            return locked
        if locked.run_id != receipt["identity"]["dispatch_key"]:
            raise RunOwnershipConflict("The admitted execution already has another worker identity.")
        if ContentFactoryRun.objects.filter(run_id=remote_run_id).exclude(pk=locked.pk).exists():
            raise RunOwnershipConflict("The worker identity is already occupied; admission is retained.")
        try:
            with transaction.atomic():
                ContentFactoryRun.objects.filter(pk=locked.pk).update(run_id=remote_run_id)
        except IntegrityError as exc:
            raise RunOwnershipConflict("The worker identity was concurrently occupied; admission is retained.") from exc
        locked.run_id = remote_run_id
        return locked
