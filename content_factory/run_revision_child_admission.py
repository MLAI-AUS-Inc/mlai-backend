"""Backend-owned review children and same-run feedback command admission.

These helpers extend already checked founder commands. They never infer the
requester from a source run's payer, copy a parent's admission into a child, or
use worker observations to create execution authority.
"""
from copy import deepcopy
import hashlib
import json
import re

from django.contrib.auth import get_user_model
from django.db import transaction

from .run_ownership import (
    BUDGET_KEY, CHILDREN_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    RunOwnershipConflict, admitted_run_for_key, bind_admitted_run,
    current_execution_ticket, owner_receipt, require_admission_identity, require_event_execution,
    reserve_admitted_run, user_principal, wire_identity,
)
from .website_contract import CONNECTION_FIELDS, connection_contract, evidence_digest, sanitized_evidence
from .website_operations import OPERATION_FIELDS

MAX_REVISION_COMMANDS = 1024
_PHYSICAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_BATCH_ID = re.compile(r"[\w-]{8,100}\Z")
_WIRE_FIELDS = ("schema_version", "client_request_id", "workflow", "source_run_id",
                WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY)


def reviewed_update_child_id(parent_run_id, operation_id, *, setup):
    """Match the worker's existing exact UTF-8 review update identity."""
    digest = hashlib.sha256(json.dumps([parent_run_id, operation_id], ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ("setup-review-" + digest[:36]) if setup else ("review-" + digest[:40])


def _principal(user):
    from core.actor_ids import preferred_actor_id_for_user
    from integrations.services.article_generation import ArticleGenerationError, _validate_authenticated_content_factory_actor
    if user is None or not get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).exists():
        raise RunOwnershipConflict("A current authenticated reviewer is required.")
    actor = preferred_actor_id_for_user(user)
    try:
        _validate_authenticated_content_factory_actor(user=user, actor_id=actor)
    except (ArticleGenerationError, ValueError) as exc:
        raise RunOwnershipConflict("The current review requester changed.") from exc
    return user_principal(user.pk, actor)


def _require_rights(user, organization, roster, *, setup=False, publish=False):
    from founder_tools import startup_team
    if roster and roster.get("frozen"):
        raise RunOwnershipConflict("This startup is read-only.")
    if startup_team._owner(organization) == user.pk:
        return
    needed = "publish" if publish else "write_articles"
    if setup or not startup_team.enabled() or needed not in startup_team._rights(user, organization, roster):
        raise RunOwnershipConflict("The current startup review permission changed.")


def _body_hash(payload):
    return evidence_digest(sanitized_evidence({key: value for key, value in payload.items()
        if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY, "child_dispatch_v1"}}))


def _attach_parent(run, payload):
    from .run_execution_control import current_execution_request
    current = current_execution_request(run)
    payload[WIRE_REQUEST_KEY] = wire_identity(owner_receipt(run))
    payload[WIRE_EXECUTION_KEY] = current_execution_ticket(run)
    payload.pop(BUDGET_KEY, None)
    if BUDGET_KEY in current:
        payload[BUDGET_KEY] = deepcopy(current[BUDGET_KEY])


def _children(run):
    value = deepcopy((run.result or {}).get(CHILDREN_RESULT_KEY, {"schema_version": 1, "dispatches": {}}))
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("dispatches"), dict)):
        raise RunOwnershipConflict("The saved child admission receipt is not readable.")
    return value


def prepare_component_revision_child(*, run, user, payload):
    """Reserve the exact operation-suffixed child before the component POST.

    The caller has already checked original billing/editorial consent and has
    reserved its existing independent revision operation. Its complete body is
    retained; only internal parent/child admission markers are added.
    """
    if owner_receipt(run) is None:
        return None
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard, scoped_run_contract
    from .website_operations import bind_operation_run, validate_operation
    from .website_models import WebsiteConnectionOperation
    principal = _principal(user)
    batch, physical = payload.get("feedback_batch_id"), payload.get("requested_run_id")
    if (type(batch) is not str or not _BATCH_ID.fullmatch(batch)
            or type(physical) is not str or not _PHYSICAL_ID.fullmatch(physical)
            or payload.get("source_run_id") != run.run_id or "child_dispatch_v1" in payload):
        raise RunOwnershipConflict("An exact server-reserved revision identity is required.")
    original = scoped_run_contract(run)
    if run.workflow not in {"article_generation", "confirmed_topic", "direct_generate", "article_revision"}:
        raise RunOwnershipConflict("Only an admitted article can create an article revision child.")
    if (payload.get("domain", run.domain) != run.domain
            or payload.get("github_repo", run.github_repo) != run.github_repo
            or payload.get("expected_source_sha") != original.get("expected_source_sha")):
        raise RunOwnershipConflict("The revision cannot replace its source repository or commit.")
    if WIRE_REQUEST_KEY in payload:
        require_admission_identity(run, {WIRE_REQUEST_KEY: payload[WIRE_REQUEST_KEY]})
    if WIRE_EXECUTION_KEY in payload:
        require_event_execution(run, payload, current=True)
    binding = connection_contract(original)
    from contextlib import nullcontext
    from .portable_drafts import original_portable_run
    if not binding and not original_portable_run(run):
        raise RunOwnershipConflict("An unbound revision requires the original explicit portable intent.")
    source_guard = authority_guard(original, action="read") if binding else nullcontext(None)
    parent_receipt = owner_receipt(run)
    parent_ticket = current_execution_ticket(run)
    key = "r3-component-child:" + evidence_digest({"parent": wire_identity(parent_receipt), "batch": batch})
    intent_hash = _body_hash(payload)
    with source_guard as website:
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster)
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if (owner_receipt(locked) != parent_receipt or current_execution_ticket(locked) != parent_ticket
                    or locked.organization_id != run.organization_id or locked.domain != run.domain
                    or locked.github_repo != run.github_repo):
                raise RunOwnershipConflict("The reviewed source changed before child admission.")
            children = _children(locked)
            previous = children["dispatches"].get(key)
            if previous is not None:
                child = admitted_run_for_key(key, lock=True)
                saved = owner_receipt(child)
                if (not isinstance(previous, dict) or saved is None
                        or type(previous.get("schema_version")) is not int or previous["schema_version"] != 1
                        or previous.get("workflow") != "article_revision" or child.workflow != "article_revision"
                        or previous.get("client_request_id") != key or previous.get("source_run_id") != locked.run_id
                        or child.run_request.get("source_run_id") != locked.run_id
                        or child.run_request.get("parent_admission_digest") != evidence_digest(wire_identity(parent_receipt))
                        or previous.get("request_hash") != intent_hash or previous.get("requested_run_id") != physical
                        or previous.get("parent_execution_id") != parent_ticket["execution_id"]
                        or child.run_id != physical or child.organization_id != locked.organization_id
                        or child.domain != locked.domain or child.github_repo != locked.github_repo
                        or saved["identity"]["principal"] != principal
                        or previous.get(WIRE_REQUEST_KEY) != wire_identity(saved)
                        or previous.get(BUDGET_KEY) != child.run_request.get(BUDGET_KEY)):
                    raise RunOwnershipConflict("This feedback identity already owns a different child.")
                ticket = previous.get(WIRE_EXECUTION_KEY)
                require_event_execution(child, previous, generation=ticket.get("generation") if isinstance(ticket, dict) else None)
                dispatch = {name: deepcopy(previous[name]) for name in _WIRE_FIELDS if name in previous}
            else:
                if len(children["dispatches"]) >= MAX_REVISION_COMMANDS:
                    raise RunOwnershipConflict("This source already has the maximum retained child commands.")
                if binding:
                    if connection_contract(payload) != binding:
                        raise RunOwnershipConflict("The revision website binding changed.")
                    validate_operation(website, {**payload, "domain": locked.domain, "github_repo": locked.github_repo})
                child_request = deepcopy({name: value for name, value in payload.items()
                    if name not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY}})
                child_request.update(domain=locked.domain, github_repo=locked.github_repo,
                    client_request_id=key, parent_run_id=locked.run_id,
                    parent_admission_digest=evidence_digest(wire_identity(parent_receipt)),
                    parent_execution_id=parent_ticket["execution_id"])
                child = reserve_admitted_run(organization=locked.organization, principal=principal,
                    workflow="article_revision", payload=child_request, reserved_run_id=physical)
                if child is None:
                    raise RunOwnershipConflict("New revision admission is disabled.")
                saved = owner_receipt(child)
                if (parent_receipt["authority"].get("event_protocol") == 1 and saved["authority"].get("event_protocol") != 1
                        or parent_receipt["authority"].get("backend_facts") is True and saved["authority"].get("backend_facts") is not True):
                    raise RunOwnershipConflict("An admitted source cannot create a weaker child execution.")
                if binding:
                    operation = WebsiteConnectionOperation.objects.select_for_update().get(pk=payload["operation_id"])
                    bind_operation_run(operation, child)
                dispatch = {"schema_version": 1, "client_request_id": key, "workflow": "article_revision",
                    "source_run_id": locked.run_id, WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                    WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY])}
                if BUDGET_KEY in child_request:
                    dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
                children["dispatches"][key] = {**deepcopy(dispatch), "request_hash": intent_hash,
                    "requested_run_id": physical, "parent_execution_id": parent_ticket["execution_id"]}
                ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, CHILDREN_RESULT_KEY: children})
                locked.refresh_from_db()
            run.result = deepcopy(locked.result)
    _attach_parent(run, payload)
    payload["child_dispatch_v1"] = dispatch
    return child


def accept_revision_child_response(child, payload):
    """Accept only the fixed backend-reserved child; never adopt a foreign ID."""
    if child is None:
        return None
    if not isinstance(payload, dict):
        raise RunOwnershipConflict("The revision response did not identify its admitted child.")
    identifier = payload.get("revisionRunId") or payload.get("newRunId") or payload.get("run_id") or payload.get("runId") or payload.get("revision_run_id")
    if identifier != child.run_id:
        raise RunOwnershipConflict("The revision response identified a different child.")
    return bind_admitted_run(child, identifier)


def prepare_setup_revision_ticket(*, run, user, payload):
    """Authorize one new generation for one exact setup feedback batch."""
    receipt = owner_receipt(run)
    if receipt is None or receipt["authority"].get("event_protocol") != 1:
        return None
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .run_execution_control import CONTROL_KEY, append_execution_ticket
    from .website_connections import authority_guard, scoped_run_contract
    principal = _principal(user)
    batch = payload.get("feedback_batch_id")
    if (run.workflow not in {"article_system_setup", "scaffold_articles"}
            or type(batch) is not str or not _BATCH_ID.fullmatch(batch) or "child_dispatch_v1" in payload):
        raise RunOwnershipConflict("An exact setup feedback batch is required.")
    key = evidence_digest({"parent": wire_identity(receipt), "reason": "user_revision", "batch": batch})
    intent_hash = _body_hash(payload)
    with authority_guard(scoped_run_contract(run), action="setup"):
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster, setup=True)
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if owner_receipt(locked) != receipt:
                raise RunOwnershipConflict("The setup execution identity changed.")
            control = deepcopy((locked.result or {}).get(CONTROL_KEY, {"schema_version": 1, "grants": {}}))
            batches = control.setdefault("revision_batches", {})
            if not isinstance(batches, dict):
                raise RunOwnershipConflict("The saved feedback commands are not readable.")
            previous = batches.get(key)
            if previous is not None:
                if (not isinstance(previous, dict) or previous.get("request_hash") != intent_hash
                        or previous.get("principal") != principal):
                    raise RunOwnershipConflict("This feedback batch already owns different changes.")
                ticket = previous.get(WIRE_EXECUTION_KEY)
                require_event_execution(locked, previous, generation=ticket.get("generation") if isinstance(ticket, dict) else None)
            else:
                if len(batches) >= MAX_REVISION_COMMANDS:
                    raise RunOwnershipConflict("This setup already has the maximum retained feedback commands.")
                ticket = append_execution_ticket(locked, control_key=key, reason="user_revision")
                control = deepcopy(locked.result[CONTROL_KEY])
                batches = control.setdefault("revision_batches", {})
                batches[key] = {"request_hash": intent_hash, "principal": principal,
                    WIRE_EXECUTION_KEY: deepcopy(ticket)}
                control["revision_batches"] = batches
                ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, CONTROL_KEY: control})
                locked.refresh_from_db()
            run.result = deepcopy(locked.result)
    _attach_parent(run, payload)
    payload[WIRE_EXECUTION_KEY] = deepcopy(ticket)
    return ticket


def prepare_review_update_child(*, run, user, payload, snapshot=None):
    """Reserve applyUpdate's deterministic child, with its actual reviewed ref.

    A pure article text edit remains on its source run. Setup updates and AI
    article changes get separate admissions before the existing HTTP dispatch.
    """
    receipt = owner_receipt(run)
    if receipt is None:
        return None
    setup = run.workflow in {"article_system_setup", "scaffold_articles"}
    if not setup and not payload.get("commentIds") and not payload.get("restoredSentences"):
        from founder_tools import startup_team
        _principal(user)
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster)
        _attach_parent(run, payload)
        return None
    from contextlib import nullcontext
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .portable_drafts import original_portable_run
    from .run_review_source import seal_review_source, verify_review_snapshot
    from .website_connections import authority_guard, scoped_run_contract
    from .website_models import WebsiteConnectionOperation
    from .website_operations import reserve_workflow_operation, bind_operation_run
    operation_id = payload.get("operationId")
    if type(operation_id) is not str or not _BATCH_ID.fullmatch(operation_id) or "child_dispatch_v1" in payload:
        raise RunOwnershipConflict("An exact saved review update is required.")
    principal = _principal(user)
    workflow = "article_system_setup" if setup else "article_revision"
    original = scoped_run_contract(run)
    binding = connection_contract(original)
    if not binding and (setup or not original_portable_run(run)):
        raise RunOwnershipConflict("This review has no original website or explicit portable consent.")
    parent_ticket = current_execution_ticket(run)
    key = "r3-reviewed-child:" + evidence_digest({"parent": wire_identity(receipt), "operation": operation_id,
        "workflow": workflow})
    physical = reviewed_update_child_id(run.run_id, operation_id, setup=setup)
    intent_hash = _body_hash(payload)
    # Only new creation needs a new snapshot. A retry reuses its exact saved
    # original source/recipe/ticket even if a later review revision is visible.
    existing = _children(run)["dispatches"].get(key)
    if binding and existing is None and callable(snapshot):
        snapshot = snapshot()
    verified = verify_review_snapshot(run, snapshot, expected_revision=payload.get("expectedRevision")) if binding and existing is None else None
    guard = authority_guard(original, action="read") if binding else nullcontext(None)
    with guard as website:
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster, setup=setup)
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if owner_receipt(locked) != receipt or current_execution_ticket(locked) != parent_ticket:
                raise RunOwnershipConflict("The review source changed before its child admission.")
            children = _children(locked)
            previous = children["dispatches"].get(key)
            if previous is not None:
                child = admitted_run_for_key(key, lock=True)
                if (child is None or owner_receipt(child) is None or not isinstance(previous, dict)
                        or previous.get("request_hash") != intent_hash or previous.get("requested_run_id") != physical
                        or child.run_id != physical or child.workflow != workflow or child.organization_id != locked.organization_id
                        or child.domain != locked.domain or child.github_repo != locked.github_repo
                        or owner_receipt(child)["identity"]["principal"] != principal
                        or child.run_request.get("parent_run_id") != locked.run_id
                        or child.run_request.get("parent_admission_digest") != evidence_digest(wire_identity(receipt))):
                    raise RunOwnershipConflict("This update identity already owns a different child.")
                require_admission_identity(child, previous)
                require_event_execution(child, previous)
                dispatch = {name: deepcopy(previous[name]) for name in _WIRE_FIELDS if name in previous}
                contract = deepcopy(previous["contract"])
            else:
                if len(children["dispatches"]) >= MAX_REVISION_COMMANDS:
                    raise RunOwnershipConflict("This source has the maximum retained review commands.")
                reservation = {**binding, "domain": run.domain, "github_repo": run.github_repo,
                    "client_request_id": key, "source_run_id": run.run_id,
                    "textEdits": payload.get("textEdits", []), "comments": payload.get("comments", []),
                    **({"expected_source_sha": original["expected_source_sha"]} if binding and original.get("expected_source_sha") else {})}
                operation = reserve_workflow_operation(website, workflow=workflow, payload=reservation) if binding else None
                child_request = deepcopy({name: value for name, value in payload.items()
                    if name not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY}})
                child_request.update({name: reservation[name] for name in (*CONNECTION_FIELDS, *OPERATION_FIELDS) if name in reservation})
                child_request.update(domain=locked.domain, github_repo=locked.github_repo, client_request_id=key,
                    source_run_id=locked.run_id, parent_run_id=locked.run_id, requested_run_id=physical,
                    parent_admission_digest=evidence_digest(wire_identity(receipt)), parent_execution_id=parent_ticket["execution_id"])
                if verified is not None:
                    child_request.update(branch=website.branch, app_root=website.app_root,
                        configuration_revision=website.configuration_version)
                    child_request.update(expected_source_sha=verified.expected_source_sha,
                        reviewed_source_ref=verified.reviewed_source_ref, reviewed_revision=verified.reviewed_revision)
                else:
                    child_request.update(delivery_mode="content_only", delivery_mode_confirmed=True, delivery_mode_explicit=True)
                child = reserve_admitted_run(organization=locked.organization, principal=principal,
                    workflow=workflow, payload=child_request, reserved_run_id=physical)
                if child is None or owner_receipt(child)["authority"].get("event_protocol") != receipt["authority"].get("event_protocol"):
                    raise RunOwnershipConflict("The review child execution cannot be admitted.")
                if operation is not None:
                    bind_operation_run(operation, child)
                    seal_review_source(parent=locked, child=child, connection=website, verified=verified)
                dispatch = {"schema_version": 1, "client_request_id": key, "workflow": workflow,
                    "source_run_id": locked.run_id, WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                    WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY])}
                if BUDGET_KEY in child_request:
                    dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
                contract = {name: deepcopy(child_request[name]) for name in (*CONNECTION_FIELDS, *OPERATION_FIELDS,
                    "expected_source_sha", "reviewed_source_ref", "reviewed_revision", "requested_run_id",
                    "branch", "app_root", "configuration_revision") if name in child_request}
                children["dispatches"][key] = {**deepcopy(dispatch), "request_hash": intent_hash,
                    "requested_run_id": physical, "contract": contract}
                ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, CHILDREN_RESULT_KEY: children})
                locked.refresh_from_db()
            run.result = deepcopy(locked.result)
    _attach_parent(run, payload)
    payload.update(contract)
    payload["requested_run_id"] = physical
    payload["child_dispatch_v1"] = dispatch
    return child


def persist_revision_metadata(run, proposed, *, keys):
    """Merge command metadata into fresh owned state without changing clocks."""
    from workflow_runs.models import ContentFactoryRun
    if owner_receipt(run) is None:
        run.result = proposed
        run.save(update_fields=["result", "updated_at"])
        return
    allowed = {"component_feedback_latest_batch", "component_feedback_revision_run_id",
        "submitted_component_comments", "source_run_id", "feedback_batch_id",
        "article_system_review_comments", "latest_article_system_revision_response"}
    if not set(keys) <= allowed:
        raise RunOwnershipConflict("Only revision command metadata may be retained.")
    with transaction.atomic():
        fresh = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = deepcopy(fresh.result or {})
        for key in keys:
            if key not in proposed:
                continue
            value = deepcopy(proposed[key])
            if key == "component_feedback_latest_batch":
                current = result.get(key) or {}
                if current.get("id") == value.get("id") and current.get("status") in {"completed", "failed", "cancelled", "ready"}:
                    value = {**value, **current}
            if key == "article_system_review_comments":
                current = list(result.get(key) or [])
                known = {(item.get("id"), item.get("feedbackBatchId")) for item in current if isinstance(item, dict)}
                value = current + [item for item in value if isinstance(item, dict)
                    and (item.get("id"), item.get("feedbackBatchId")) not in known]
            result[key] = value
        ContentFactoryRun.objects.filter(pk=fresh.pk).update(result=result)
        run.refresh_from_db()
