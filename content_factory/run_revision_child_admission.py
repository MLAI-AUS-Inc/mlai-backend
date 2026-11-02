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


def require_revision_requester(run, user, *, setup=False, publish=False):
    """Check current requester before a new-protocol provider/operation preflight."""
    from .run_ownership import admission_enabled
    if owner_receipt(run) is None and not admission_enabled():
        child_id = (run.result or {}).get("publish_child_run_id") or (run.result or {}).get("promoted_publish_job_id") if publish else None
        if not child_id:
            return
        from workflow_runs.models import ContentFactoryRun
        child = ContentFactoryRun.objects.filter(run_id=child_id, organization_id=run.organization_id).first()
        if child is None or owner_receipt(child) is None:
            return
    _require_original_request(run)
    from founder_tools import startup_team
    _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, setup=setup, publish=publish)


def _require_original_request(run):
    from .run_ownership import user_stop_intent
    if user_stop_intent(run) or run.status in {"cancelled", "denied"}:
        raise RunOwnershipConflict("The source requester withdrew this execution consent.")
    receipt = owner_receipt(run)
    if receipt is not None and evidence_digest({key: value for key, value in (run.run_request or {}).items()
            if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}) != receipt["identity"]["request_digest"]:
        raise RunOwnershipConflict("The admitted original request changed.")


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
        batch = payload.get("feedback_batch_id")
        if type(batch) is not str or not _BATCH_ID.fullmatch(batch):
            from .run_ownership import admission_enabled
            if admission_enabled():
                raise RunOwnershipConflict("An exact saved feedback batch is required.")
            return None
        return _prepare_legacy_child(run=run, user=user, payload=payload, workflow="article_revision",
            physical=payload.get("requested_run_id"), intent="component:" + batch)
    _require_original_request(run)
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard, scoped_run_contract
    from .website_operations import bind_operation_run, validate_operation
    from .website_models import WebsiteConnectionOperation
    principal = _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster)
    batch, physical = payload.get("feedback_batch_id"), payload.get("requested_run_id")
    if (type(batch) is not str or not _BATCH_ID.fullmatch(batch)
            or type(physical) is not str or not _PHYSICAL_ID.fullmatch(physical)
            or payload.get("source_run_id") != run.run_id
            or any(field in payload for field in ("child_dispatch_v1", "legacy_source_v1", "review_source_v1", "reviewed_publication_source_v1"))):
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
    parent_receipt = owner_receipt(run)
    parent_ticket = current_execution_ticket(run)
    key = "r3-component-child:" + evidence_digest({"parent": wire_identity(parent_receipt), "batch": batch})
    intent_hash = _body_hash(payload)
    source_guard = authority_guard(original, action="read", require_current_head=admitted_run_for_key(key) is None) if binding else nullcontext(None)
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
                if original_portable_run(locked):
                    child_request.update({field: original[field] for field in ("delivery_mode", "delivery_mode_confirmed",
                        "delivery_mode_explicit", "resolved_delivery_mode") if field in original})
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
    identifier = (payload.get("revisionRunId") or payload.get("newRunId") or payload.get("child_run_id")
        or payload.get("run_id") or payload.get("runId") or payload.get("revision_run_id") or payload.get("job_id"))
    if identifier != child.run_id:
        raise RunOwnershipConflict("The revision response identified a different child.")
    return bind_admitted_run(child, identifier)


def prepare_setup_revision_ticket(*, run, user, payload):
    """Authorize one new generation for one exact setup feedback batch."""
    receipt = owner_receipt(run)
    if receipt is None or receipt["authority"].get("event_protocol") != 1:
        return None
    _require_original_request(run)
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .run_execution_control import CONTROL_KEY, append_execution_ticket
    from .website_connections import authority_guard, scoped_run_contract
    principal = _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, setup=True)
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
        setup = run.workflow in {"article_system_setup", "scaffold_articles"}
        if not setup and not payload.get("commentIds") and not payload.get("restoredSentences"):
            return None
        operation = payload.get("operationId")
        from .run_ownership import admission_enabled
        if type(operation) is not str or not _BATCH_ID.fullmatch(operation):
            if not admission_enabled():
                return None
            raise RunOwnershipConflict("An exact saved review update is required.")
        return _prepare_legacy_child(run=run, user=user, payload=payload,
            workflow="article_system_setup" if setup else "article_revision",
            physical=reviewed_update_child_id(run.run_id, operation, setup=setup),
            intent="review:" + operation, setup=setup, snapshot=snapshot)
    _require_original_request(run)
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
    if (type(operation_id) is not str or not _BATCH_ID.fullmatch(operation_id)
            or any(field in payload for field in ("child_dispatch_v1", "legacy_source_v1", "review_source_v1", "reviewed_publication_source_v1"))):
        raise RunOwnershipConflict("An exact saved review update is required.")
    principal = _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, setup=setup)
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
    verified = None
    if binding and existing is None:
        snapshot = snapshot() if callable(snapshot) else snapshot
        if setup:
            verified = verify_review_snapshot(run, snapshot, expected_revision=payload.get("expectedRevision"))
        elif not isinstance(snapshot, dict) or snapshot.get("revision") != payload.get("expectedRevision"):
            raise RunOwnershipConflict("Reload the current canonical review before updating it.")
        elif snapshot.get("baseCommit") or snapshot.get("sourceRef"):
            verified = verify_review_snapshot(run, snapshot, expected_revision=payload.get("expectedRevision"))
    guard = authority_guard(original, action="read", require_current_head=existing is None and verified is None) if binding else nullcontext(None)
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
                if binding:
                    child_request.update(branch=website.branch, app_root=website.app_root,
                        configuration_revision=website.configuration_version,
                        expected_source_sha=original.get("expected_source_sha"))
                    if verified is not None:
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
                    if verified is not None:
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


def persist_revision_metadata(run, proposed, *, keys, admitted_child=False):
    """Merge command metadata into fresh owned state without changing clocks."""
    from workflow_runs.models import ContentFactoryRun
    if owner_receipt(run) is None and not admitted_child:
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
                if current.get("id") == value.get("id") and (current.get("status") in {"completed", "failed", "cancelled", "ready"}
                        or current.get("status") == "running" and value.get("status") == "submitted"):
                    value = {**value, **current}
            if key == "article_system_review_comments":
                current = list(result.get(key) or [])
                known = {(item.get("id"), item.get("feedbackBatchId")) for item in current if isinstance(item, dict)}
                value = current + [item for item in value if isinstance(item, dict)
                    and (item.get("id"), item.get("feedbackBatchId")) not in known]
            result[key] = value
        ContentFactoryRun.objects.filter(pk=fresh.pk).update(result=result)
        run.refresh_from_db()


def _prepare_legacy_child(*, run, user, payload, workflow, physical, intent, setup=False, snapshot=None):
    """Admit only a new child; retain every byte of its historical parent."""
    from contextlib import nullcontext
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .run_legacy_source import fetch_legacy_source
    from .run_ownership import admission_enabled, CHILDREN_RESULT_KEY
    from .portable_drafts import original_portable_run
    from .run_review_source import verify_review_snapshot, seal_review_source
    from .website_connections import authority_guard, scoped_run_contract
    from .website_operations import reserve_workflow_operation, validate_operation, bind_operation_run
    from .website_models import WebsiteConnectionOperation
    if not admission_enabled() and (getattr(run, "_state", None) is None or run._state.adding):
        return None
    key = "r3-legacy-child:" + evidence_digest({"organization_id": str(run.organization_id),
        "source_run_id": run.run_id, "workflow": workflow, "intent": intent})
    existing = admitted_run_for_key(key)
    if not admission_enabled() and existing is None:
        return None
    if (run.workflow not in {"article_system_setup", "scaffold_articles", "article_generation", "confirmed_topic", "direct_generate", "article_revision"}
            or payload.get("source_run_id") != run.run_id or "legacy_source_v1" in payload
            or payload.get("domain", run.domain) != run.domain or payload.get("github_repo", run.github_repo) != run.github_repo):
        raise RunOwnershipConflict("The command does not match its retained source.")
    _require_original_request(run)
    principal = _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, setup=setup)
    original = scoped_run_contract(run)
    binding = connection_contract(original)
    if binding:
        from .website_contract import SHA_PATTERN
        if "repository_id" not in binding or not SHA_PATTERN.fullmatch(str(original.get("expected_source_sha") or "")):
            raise RunOwnershipConflict("This historical source needs a newly verified website scan.")
    elif setup or not original_portable_run(run):
        raise RunOwnershipConflict("This source has no retained website or explicit portable intent.")
    if ("child_dispatch_v1" in payload or WIRE_REQUEST_KEY in payload or WIRE_EXECUTION_KEY in payload
            or type(physical) is not str or not _PHYSICAL_ID.fullmatch(physical)):
        raise RunOwnershipConflict("A caller cannot supply a new child's authority.")
    parent_request_digest = evidence_digest(run.run_request or {})
    descriptor = fetch_legacy_source(run)
    intent_hash = _body_hash(payload)
    verified = None
    if snapshot is not None and binding and existing is None:
        snapshot = snapshot() if callable(snapshot) else snapshot
        if setup:
            verified = verify_review_snapshot(run, snapshot, expected_revision=payload.get("expectedRevision"), legacy_source=descriptor)
        elif not isinstance(snapshot, dict) or snapshot.get("revision") != payload.get("expectedRevision"):
            raise RunOwnershipConflict("Reload the current canonical review before updating it.")
        elif snapshot.get("baseCommit") or snapshot.get("sourceRef"):
            verified = verify_review_snapshot(run, snapshot, expected_revision=payload.get("expectedRevision"), legacy_source=descriptor)
    guard = authority_guard(original, action="read", require_current_head=existing is None and verified is None) if binding else nullcontext(None)
    with guard as website:
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster, setup=setup)
            parent = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if (owner_receipt(parent) is not None or parent.organization_id != run.organization_id
                    or parent.domain != run.domain or parent.github_repo != run.github_repo
                    or evidence_digest(parent.run_request or {}) != parent_request_digest):
                raise RunOwnershipConflict("The retained legacy source changed before child admission.")
            child = admitted_run_for_key(key, lock=True)
            if child is not None:
                saved = deepcopy((child.result or {}).get(CHILDREN_RESULT_KEY, {}).get("source_dispatch"))
                if (owner_receipt(child) is None or not isinstance(saved, dict)
                        or saved.get("request_hash") != intent_hash or child.run_id != physical
                        or child.workflow != workflow or child.organization_id != parent.organization_id
                        or child.domain != parent.domain or child.github_repo != parent.github_repo
                        or child.run_request.get("legacy_source_v1") != descriptor
                        or child.run_request.get("parent_run_id") != parent.run_id
                        or owner_receipt(child)["identity"]["principal"] != principal):
                    raise RunOwnershipConflict("This command already owns a different legacy-source child.")
                dispatch, contract = deepcopy(saved["dispatch"]), deepcopy(saved["contract"])
                require_admission_identity(child, dispatch)
                require_event_execution(child, dispatch)
            else:
                child_request = deepcopy(payload)
                child_request.update(domain=parent.domain, github_repo=parent.github_repo, source_run_id=parent.run_id,
                    parent_run_id=parent.run_id, client_request_id=key, legacy_source_v1=descriptor)
                if binding:
                    if snapshot is not None:
                        reservation = {**binding, "domain": parent.domain, "github_repo": parent.github_repo,
                            "client_request_id": key, "source_run_id": parent.run_id,
                            "expected_source_sha": original["expected_source_sha"],
                            "comments": payload.get("comments", []), "textEdits": payload.get("textEdits", [])}
                        operation = reserve_workflow_operation(website, workflow=workflow, payload=reservation)
                        child_request.update({field: reservation[field] for field in OPERATION_FIELDS})
                    else:
                        if connection_contract(payload) != binding or payload.get("expected_source_sha") != original["expected_source_sha"]:
                            raise RunOwnershipConflict("The legacy source website identity changed.")
                        validate_operation(website, child_request)
                        operation = WebsiteConnectionOperation.objects.select_for_update().get(pk=child_request["operation_id"])
                    child_request.update(binding)
                    child_request.update(expected_source_sha=original["expected_source_sha"], branch=website.branch,
                        app_root=website.app_root, configuration_revision=website.configuration_version)
                    if verified is not None:
                        child_request.update(expected_source_sha=verified.expected_source_sha,
                            reviewed_source_ref=verified.reviewed_source_ref, reviewed_revision=verified.reviewed_revision,
                            branch=website.branch, app_root=website.app_root, configuration_revision=website.configuration_version)
                else:
                    child_request.update({field: original[field] for field in ("delivery_mode", "delivery_mode_confirmed",
                        "delivery_mode_explicit", "resolved_delivery_mode") if field in original})
                child_request["requested_run_id"] = physical
                child = reserve_admitted_run(organization=parent.organization, principal=principal,
                    workflow=workflow, payload=child_request, reserved_run_id=physical)
                if child is None:
                    raise RunOwnershipConflict("New child admission is disabled.")
                if binding:
                    bind_operation_run(operation, child)
                    if verified is not None:
                        seal_review_source(parent=parent, child=child, connection=website, verified=verified)
                dispatch = {"schema_version": 1, "client_request_id": key, "workflow": workflow,
                    "source_run_id": parent.run_id, "legacy_source_v1": deepcopy(descriptor),
                    WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                    WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY])}
                if BUDGET_KEY in child_request:
                    dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
                contract = {field: deepcopy(child_request[field]) for field in (*CONNECTION_FIELDS, *OPERATION_FIELDS,
                    "expected_source_sha", "reviewed_source_ref", "reviewed_revision", "requested_run_id",
                    "branch", "app_root", "configuration_revision") if field in child_request}
                source_dispatch = {"request_hash": intent_hash, "dispatch": deepcopy(dispatch), "contract": deepcopy(contract)}
                result = {**child.result, CHILDREN_RESULT_KEY: {"schema_version": 1, "dispatches": {}, "source_dispatch": source_dispatch}}
                ContentFactoryRun.objects.filter(pk=child.pk).update(result=result)
                child.result = result
    # The parent never acquires admission or a backend child registry. Only the
    # new child is enrolled, and both parent echoes truthfully remain absent.
    payload.update(contract)
    payload["child_dispatch_v1"] = dispatch
    return child


def prepare_publish_child(*, run, user, payload, action):
    """Reserve a separately admitted publication of this exact approved review."""
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .models import OrganizationContentConfig
    from .article_publish_approval import article_publish_approval_receipt_matches, article_publish_approval_receipt
    from .run_legacy_source import fetch_legacy_source
    from .run_ownership import admission_enabled
    from .incident_guards import publish_child_binding
    from .website_connections import authority_guard, scoped_run_contract
    from .website_operations import reserve_workflow_operation, bind_operation_run
    receipt = owner_receipt(run)
    if action not in {"promote-bundle", "publish-pr"}:
        raise RunOwnershipConflict("Only an explicit reviewed publication can reserve this child.")
    if receipt is None and not admission_enabled() and (getattr(run, "_state", None) is None or run._state.adding):
        return None
    approval = article_publish_approval_receipt(run)
    key = "r3-publish-child:" + evidence_digest({"organization_id": str(run.organization_id),
        "source_run_id": run.run_id, "review": approval})
    existing = admitted_run_for_key(key)
    if receipt is None and not admission_enabled() and existing is None:
        return None
    if (run.workflow not in {"article_generation", "confirmed_topic", "direct_generate", "article_revision"}
            or not article_publish_approval_receipt_matches(run) or "child_dispatch_v1" in payload
            or "requested_child_run_id" in payload or "reviewed_publication_source_v1" in payload
            or "legacy_source_v1" in payload or "review_source_v1" in payload):
        raise RunOwnershipConflict("Review and approve this exact article before publishing it.")
    _require_original_request(run)
    principal = _principal(user)
    original = scoped_run_contract(run)
    request_digest = evidence_digest(run.run_request or {})
    descriptor = fetch_legacy_source(run) if receipt is None else None
    evidence = _publication_evidence(run, user=user, descriptor=descriptor)
    from .run_review_source import publication_source_scope
    with publication_source_scope(run, evidence):
        config = OrganizationContentConfig.objects.select_related("website_connection").get(organization=run.organization)
        destination = publish_child_binding(config, run, payload, {})
        guard_payload = {**destination, "domain": run.domain, "github_repo": run.github_repo}
        physical = "publish-child-" + evidence_digest({"key": key})[:48]
        intent_hash = _body_hash(payload)
        with authority_guard(guard_payload, action="publish") as website:
            with startup_team._locked(run.organization) as (_, roster):
                _require_rights(user, run.organization, roster, publish=True)
                parent = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                if (owner_receipt(parent) != receipt or evidence_digest(parent.run_request or {}) != request_digest
                        or not article_publish_approval_receipt_matches(parent)
                        or article_publish_approval_receipt(parent) != approval
                        or receipt is not None and evidence_digest(current_execution_ticket(parent)) != evidence["source_ticket_digest"]):
                    raise RunOwnershipConflict("The approved source changed before publication admission.")
                config.refresh_from_db()
                if publish_child_binding(config, parent, payload, {}) != destination:
                    raise RunOwnershipConflict("The selected publishing destination changed.")
                child = admitted_run_for_key(key, lock=True)
                if child is not None:
                    saved = deepcopy((child.result or {}).get(CHILDREN_RESULT_KEY, {}).get("source_dispatch"))
                    if (owner_receipt(child) is None or not isinstance(saved, dict) or saved.get("request_hash") != intent_hash
                            or child.run_id != physical or child.organization_id != parent.organization_id
                            or child.github_repo != website.github_repo or child.workflow != "article_generation"
                            or owner_receipt(child)["identity"]["principal"] != principal
                            or child.run_request.get("source_run_id") != parent.run_id
                            or child.run_request.get("approved_review_receipt") != approval
                            or child.run_request.get("reviewed_publication_source_v1") != evidence
                            or child.run_request.get("legacy_source_v1") != descriptor):
                        raise RunOwnershipConflict("This approved review already owns a different publication child.")
                    dispatch, contract = deepcopy(saved["dispatch"]), deepcopy(saved["contract"])
                    require_admission_identity(child, dispatch)
                    require_event_execution(child, dispatch)
                else:
                    reservation = {**destination, "domain": parent.domain, "github_repo": website.github_repo,
                        "client_request_id": key, "source_run_id": parent.run_id,
                        "approved_review_receipt": deepcopy(approval), "delivery_mode": "publish_code", "delivery_mode_confirmed": True}
                    operation = reserve_workflow_operation(website, workflow="article_generation", payload=reservation)
                    child_request = deepcopy({field: value for field, value in parent.run_request.items()
                        if field not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY, "run_id", "job_id", "reviewed_source_ref", "reviewed_revision", "legacy_source_v1"}})
                    child_request.update(deepcopy(payload))
                    for old_flag in ("publish_auto_merge", "autoMerge"):
                        child_request.pop(old_flag, None)
                    child_request["auto_merge"] = payload.get("auto_merge") is True
                    child_request.update(reservation)
                    child_request.update(reviewed_publication_source_v1=deepcopy(evidence),
                        branch=website.branch, app_root=website.app_root, configuration_revision=website.configuration_version)
                    child_request.update(requested_child_run_id=physical, parent_run_id=parent.run_id,
                        original_billing_source_run_id=parent.run_request.get("original_billing_source_run_id") or parent.run_id)
                    if receipt is not None:
                        child_request["parent_admission_digest"] = evidence_digest(wire_identity(receipt))
                    else:
                        child_request["legacy_source_v1"] = deepcopy(descriptor)
                    child = reserve_admitted_run(organization=parent.organization, principal=principal,
                        workflow="article_generation", payload=child_request, reserved_run_id=physical)
                    if child is None:
                        raise RunOwnershipConflict("The new publication child cannot be admitted.")
                    bind_operation_run(operation, child)
                    dispatch = {"schema_version": 1, "client_request_id": key, "workflow": "article_generation",
                        "source_run_id": parent.run_id, WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                        WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY]),
                        "reviewed_publication_source_v1": deepcopy(evidence)}
                    if descriptor is not None:
                        dispatch["legacy_source_v1"] = deepcopy(descriptor)
                    if BUDGET_KEY in child_request:
                        dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
                    contract = {field: deepcopy(child_request[field]) for field in (*CONNECTION_FIELDS, *OPERATION_FIELDS,
                        "expected_source_sha", "requested_child_run_id", "publish_target_id", "article_slug", "route_path") if field in child_request}
                    saved = {"request_hash": intent_hash, "dispatch": deepcopy(dispatch), "contract": deepcopy(contract)}
                    result = {**child.result, CHILDREN_RESULT_KEY: {"schema_version": 1, "dispatches": {}, "source_dispatch": saved}}
                    ContentFactoryRun.objects.filter(pk=child.pk).update(result=result)
                    child.result = result
        if receipt is not None:
            _attach_parent(parent, payload)
        payload.update(contract)
        payload["child_dispatch_v1"] = dispatch
        return child


def checked_publish_child(*, run, user, payload):
    """Recheck the admitted child and current publisher at the HTTP boundary."""
    from founder_tools import startup_team
    from .website_connections import authority_guard, scoped_run_contract
    dispatch = payload.get("child_dispatch_v1")
    child = admitted_run_for_key(dispatch.get("client_request_id")) if isinstance(dispatch, dict) else None
    if child is None or child.run_request.get("source_run_id") != run.run_id or child.organization_id != run.organization_id:
        raise RunOwnershipConflict("The publication child changed before dispatch.")
    if owner_receipt(child)["identity"]["principal"] != _principal(user):
        raise RunOwnershipConflict("The publication requester changed.")
    require_admission_identity(child, dispatch)
    require_event_execution(child, dispatch)
    from .article_publish_approval import article_publish_approval_receipt, article_publish_approval_receipt_matches
    if (not article_publish_approval_receipt_matches(run)
            or child.run_request.get("approved_review_receipt") != article_publish_approval_receipt(run)):
        raise RunOwnershipConflict("The approved publication source changed.")
    if owner_receipt(run) is not None:
        require_admission_identity(run, payload)
        require_event_execution(run, payload, current=True)
    with authority_guard(scoped_run_contract(child), action="publish"):
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster, publish=True)
    return child


def record_publish_handoff(*, run, child, pending):
    """Keep a UI command receipt without writing execution facts or clocks."""
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        fresh = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = deepcopy(fresh.result or {})
        result.update(publish_handoff_pending=pending, publish_handoff_stale=False,
            publish_handoff_status="pending" if pending else "queued")
        if not pending:
            result.update(publish_child_run_id=child.run_id, promoted_publish_job_id=child.run_id)
        ContentFactoryRun.objects.filter(pk=fresh.pk).update(result=result)
        run.refresh_from_db()


def _publication_evidence(run, *, user, descriptor=None):
    """Verify actual retained review content before a new publishing execution."""
    from founder_tools import startup_team
    from .article_review_views import remote_review
    from .article_publish_approval import article_publish_approval_receipt, article_publish_approval_receipt_matches
    from .website_connections import scoped_run_contract, verify_repository_head, require_unlocked_remote_call
    from .website_contract import validate_authority, SHA_PATTERN
    from .website_operations import validate_operation
    from .website_models import WebsiteConnection
    from .run_review_source import verify_review_ref, _candidate
    _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, publish=True)
    original = scoped_run_contract(run)
    binding = connection_contract(original)
    if not binding or "repository_id" not in binding or not SHA_PATTERN.fullmatch(str(original.get("expected_source_sha") or "")):
        raise RunOwnershipConflict("A fresh reviewed publishing source is required.")
    connection = WebsiteConnection.objects.select_related("organization").get(pk=binding["website_connection_id"])
    validate_authority(connection, original, action="read", domain=run.domain, github_repo=run.github_repo)
    validate_operation(connection, original)
    if not article_publish_approval_receipt_matches(run):
        raise RunOwnershipConflict("Approve the exact current review before publishing.")
    require_unlocked_remote_call()
    snapshot = remote_review(run)
    if (not isinstance(snapshot, dict) or type(snapshot.get("revision")) is not str
            or not snapshot["revision"] or len(snapshot["revision"]) > 200 or snapshot.get("approved") is not True
            or snapshot.get("previewPending") or snapshot.get("pendingUpdate")):
        raise RunOwnershipConflict("Open the current ready review and approve it before publishing.")
    approval = article_publish_approval_receipt(run)
    if snapshot.get("previewCommit") and snapshot["previewCommit"] != approval.get("commit_sha"):
        raise RunOwnershipConflict("The approved preview changed before publishing.")
    source_ref, source_sha = snapshot.get("sourceRef"), snapshot.get("baseCommit")
    if source_ref is not None or source_sha is not None:
        if source_sha != approval.get("commit_sha"):
            raise RunOwnershipConflict("The reviewed repository commit changed.")
        verify_review_ref(connection, source_ref, source_sha)
    else:
        # Ordinary article snapshots have no repository ref. Keep the original
        # exact source pin; a new field in the request cannot invent a ref.
        source_ref, source_sha = None, original["expected_source_sha"]
        verify_repository_head(connection, source_sha)
    receipt = owner_receipt(run)
    proof_digest = None
    if original.get("reviewed_source_ref"):
        current = _candidate(original, connection, require_principal=False)
        proof_digest = evidence_digest(current["proof"])
    ticket = current_execution_ticket(run) if receipt else None
    evidence = {"schema_version": 1, "source_run_id": run.run_id,
        "reviewed_revision": snapshot["revision"], "reviewed_source_ref": source_ref,
        "expected_source_sha": source_sha,
        "source_admission_digest": evidence_digest(wire_identity(receipt)) if receipt else None,
        "source_execution_id": ticket["execution_id"] if ticket else None,
        "source_ticket_digest": evidence_digest(ticket) if ticket else None,
        "review_source_proof_digest": proof_digest}
    if receipt is None:
        from .run_legacy_source import validate_legacy_source
        # The descriptor is a sibling in the original child request/dispatch.
        # Keep this closed source-evidence schema identical for both lineages.
        validate_legacy_source(run, descriptor)
    return evidence


def checked_review_child(*, run, user, payload):
    """Bind a retained HTTP retry to its actual original child requester."""
    from founder_tools import startup_team
    dispatch = payload.get("child_dispatch_v1")
    child = admitted_run_for_key(dispatch.get("client_request_id")) if isinstance(dispatch, dict) else None
    principal = _principal(user)
    if (child is None or owner_receipt(child) is None or child.organization_id != run.organization_id
            or child.run_request.get("parent_run_id") != run.run_id
            or owner_receipt(child)["identity"]["principal"] != principal):
        raise RunOwnershipConflict("This saved review command belongs to a different requester.")
    require_admission_identity(child, dispatch)
    require_event_execution(child, dispatch)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, setup=child.workflow == "article_system_setup")
    return child


def _publish_approval_context(run, *, lock=False):
    from workflow_runs.models import ContentFactoryRun
    from .article_publish_approval import article_publish_approval_receipt, article_publish_approval_receipt_matches
    evidence = (run.run_request or {}).get("reviewed_publication_source_v1")
    source = run
    if evidence is not None:
        query = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
        source = query.filter(run_id=evidence.get("source_run_id"), organization_id=run.organization_id).first() if isinstance(evidence, dict) else None
        if source is None or (run.run_request or {}).get("approved_review_receipt") != article_publish_approval_receipt(source):
            raise RunOwnershipConflict("The publication review changed.")
    if not article_publish_approval_receipt_matches(source):
        raise RunOwnershipConflict("The approved publication body is no longer current.")
    return {"source_run_id": source.run_id, "approved_review_receipt": deepcopy(article_publish_approval_receipt(source)),
        "reviewed_publication_source_v1": deepcopy(evidence),
        "approved_body_sha256": (run.run_request or {}).get("approved_body_sha256")}


def record_publish_auto_merge_control(*, run, user, enabled=True):
    """Retain explicit current-publisher consent outside the immutable recipe."""
    from founder_tools import startup_team
    from workflow_runs.models import ContentFactoryRun
    from .run_ownership import ARTICLE_PUBLISH_CONTROL_RESULT_KEY
    if type(enabled) is not bool:
        raise RunOwnershipConflict("An exact publication choice is required.")
    principal = _principal(user)
    with startup_team._locked(run.organization) as (_, roster):
        _require_rights(user, run.organization, roster, publish=True)
        locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        _require_original_request(locked)
        receipt = owner_receipt(locked)
        if receipt is None:
            raise RunOwnershipConflict("This publication has no original backend admission.")
        control = {"schema_version": 1, "enabled": enabled,
            "admission_digest": evidence_digest(wire_identity(receipt)),
            "execution": current_execution_ticket(locked), "requester": principal,
            "approval_context": _publish_approval_context(locked, lock=True)}
        history = deepcopy((locked.result or {}).get(ARTICLE_PUBLISH_CONTROL_RESULT_KEY,
            {"schema_version": 1, "controls": {}, "current_control": None}))
        if (not isinstance(history, dict) or type(history.get("schema_version")) is not int or history["schema_version"] != 1
                or not isinstance(history.get("controls"), dict)):
            raise RunOwnershipConflict("The saved publication controls are not readable.")
        key = evidence_digest(control)
        if key not in history["controls"] and len(history["controls"]) >= MAX_REVISION_COMMANDS:
            raise RunOwnershipConflict("This publication has its maximum retained controls.")
        history["controls"][key] = control
        history["current_control"] = key
        ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, ARTICLE_PUBLISH_CONTROL_RESULT_KEY: history})
        run.refresh_from_db()
    return run


def publish_auto_merge_control(run):
    """Read only protected current consent, with fresh actual publisher rights."""
    from django.contrib.auth import get_user_model
    from founder_tools import startup_team
    from .run_ownership import ARTICLE_PUBLISH_CONTROL_RESULT_KEY
    receipt = owner_receipt(run)
    if receipt is None:
        return None
    try:
        _require_original_request(run)
        ticket = current_execution_ticket(run)
        context = _publish_approval_context(run)
        history = (run.result or {}).get(ARTICLE_PUBLISH_CONTROL_RESULT_KEY)
        if history is None:
            # The original canonical bool is already digest-bound to the actual
            # admitted requester. A later generation needs refreshed consent.
            request = run.run_request or {}
            if (request.get("auto_merge") is not True or ticket != request.get(WIRE_EXECUTION_KEY)):
                return False
            principal = receipt["identity"]["principal"]
        else:
            if not isinstance(history, dict) or type(history.get("schema_version")) is not int or history["schema_version"] != 1:
                return False
            key = history.get("current_control")
            control = history.get("controls", {}).get(key)
            if (not isinstance(control, dict) or key != evidence_digest(control) or control.get("enabled") is not True
                    or control.get("admission_digest") != evidence_digest(wire_identity(receipt))
                    or evidence_digest(control.get("execution")) != evidence_digest(ticket)
                    or evidence_digest(control.get("approval_context")) != evidence_digest(context)):
                return False
            principal = control.get("requester")
        if not isinstance(principal, dict) or principal.get("kind") != "user":
            return False
        user = get_user_model().objects.filter(pk=principal.get("user_id"), is_active=True).first()
        if user is None or _principal(user) != principal:
            return False
        with startup_team._locked(run.organization) as (_, roster):
            _require_rights(user, run.organization, roster, publish=True)
        return True
    except (RunOwnershipConflict, ValueError, AttributeError, KeyError, TypeError):
        return False
