"""Reserve the independent setup child before approving an admitted scan."""
from copy import deepcopy

from .run_ownership import (
    BUDGET_KEY, CHILDREN_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    RunOwnershipConflict, admission_enabled, admitted_run_for_key, current_execution_ticket, owner_receipt, require_event_execution, reserve_admitted_run,
    user_principal, wire_identity,
)
from .website_contract import CONNECTION_FIELDS, evidence_digest


def prepare_scan_child_dispatch(*, run, user, actor_id, payload):
    """Attach only server-derived child consent after fresh current setup rights.

    The caller performs its existing action gates. This helper adds current
    actual-requester validation and a separate child admission; it never
    approves or inherits a worker's plan/body/child identity from the request.
    Existing unadmitted scans keep their original continuation protocol.
    """
    receipt = owner_receipt(run)
    if receipt is None:
        saved = (run.result or {}).get(CHILDREN_RESULT_KEY, {})
        dispatches = saved.get("dispatches", {}) if isinstance(saved, dict) else None
        prior = isinstance(dispatches, dict) and any(isinstance(item, dict) and "legacy_source_v1" in item
            for item in dispatches.values())
        if not admission_enabled() and not prior:
            return None
        return _prepare_legacy_scan_child_dispatch(run=run, user=user, actor_id=actor_id, payload=payload)
    from integrations.services.article_generation import ArticleGenerationError, _validate_authenticated_content_factory_actor
    from founder_tools import startup_team
    from .run_execution_control import current_execution_request
    from .website_connections import authority_guard, dispatch_contract
    from .website_operations import reserve_workflow_operation, bind_operation_run
    from workflow_runs.models import ContentFactoryRun
    if (run.workflow not in {"scan", "repo_scan", "content_factory_scan"}
            or user is None or not user.is_active):
        raise RunOwnershipConflict("A current admitted scan and requester are required.")
    try:
        _validate_authenticated_content_factory_actor(user=user, actor_id=actor_id)
    except (ArticleGenerationError, ValueError) as exc:
        raise RunOwnershipConflict("The current setup requester changed.") from exc
    if "child_dispatch_v1" in payload:
        raise RunOwnershipConflict("A caller cannot supply a child execution identity.")
    # Provider/native/source checks happen before the local write transaction.
    binding = dispatch_contract(run.domain, {}, action="setup", source_run_id=run.run_id)
    current_request = current_execution_request(run)
    binding.update(domain=run.domain, github_repo=run.github_repo)
    with authority_guard(binding, action="setup", require_current_head=True) as connection:
        with startup_team._locked(run.organization) as (_, roster):
            if (roster and roster.get("frozen") or startup_team._owner(run.organization) != user.pk):
                raise RunOwnershipConflict("The current startup owner must approve its setup.")
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            saved = owner_receipt(locked)
            if (saved != receipt or locked.run_id != run.run_id or locked.organization_id != connection.organization_id
                    or locked.status in {"cancelled", "denied", "failed", "blocked"}
                    or current_execution_ticket(locked) != current_execution_ticket(run)
                    or current_execution_request(locked) != current_request):
                raise RunOwnershipConflict("The approved scan changed before child admission.")
            parent_wire = wire_identity(saved)
            parent_ticket = current_execution_ticket(locked)
            key = "r3-scan-child:" + evidence_digest({"parent_admission": parent_wire,
                "parent_execution": parent_ticket, "workflow": "scaffold_articles"})
            children = deepcopy((locked.result or {}).get(CHILDREN_RESULT_KEY,
                {"schema_version": 1, "dispatches": {}}))
            if (not isinstance(children, dict) or children.get("schema_version") != 1
                    or not isinstance(children.get("dispatches"), dict)):
                raise RunOwnershipConflict("The saved child admission receipt is not readable.")
            previous = children["dispatches"].get(key)
            if previous is not None:
                # Original dispatch retries are observations, including after
                # the child has resumed or completed. Never reserve another
                # operation, rehash its current attempt or replace its proof.
                child = admitted_run_for_key(key, lock=True)
                child_owner = owner_receipt(child)
                old_ticket = previous.get(WIRE_EXECUTION_KEY) if isinstance(previous, dict) else None
                if (child_owner is None or not isinstance(old_ticket, dict)
                        or previous.get("client_request_id") != key or previous.get("workflow") != "scaffold_articles"
                        or previous.get("source_run_id") != locked.run_id
                        or previous.get(WIRE_REQUEST_KEY) != wire_identity(child_owner)
                        or child_owner["identity"]["principal"] != user_principal(user.pk, actor_id)
                        or child.organization_id != locked.organization_id or child.domain != locked.domain
                        or child.workflow != "scaffold_articles" or child.status in {"cancelled", "denied"}
                        or child.run_request.get("parent_run_id") != locked.run_id
                        or child.run_request.get("parent_admission_digest") != evidence_digest(parent_wire)
                        or previous.get(BUDGET_KEY) != child.run_request.get(BUDGET_KEY)):
                    raise RunOwnershipConflict("The original child dispatch no longer matches this approval.")
                require_event_execution(child, previous, generation=old_ticket.get("generation"))
                payload.update({WIRE_REQUEST_KEY: parent_wire, WIRE_EXECUTION_KEY: parent_ticket,
                                "child_dispatch_v1": deepcopy(previous)})
                return child
            child_request = {key: value for key, value in binding.items()
                             if key in {*CONNECTION_FIELDS, "expected_source_sha"}}
            child_request.update(domain=locked.domain, github_repo=locked.github_repo, client_request_id=key,
                parent_run_id=locked.run_id, scan_run_id=locked.run_id, source_run_id=locked.run_id,
                parent_admission_digest=evidence_digest(parent_wire), parent_execution_id=parent_ticket["execution_id"])
            operation = reserve_workflow_operation(connection, workflow="scaffold_articles", payload=child_request)
            child = reserve_admitted_run(organization=locked.organization,
                principal=user_principal(user.pk, actor_id), workflow="scaffold_articles", payload=child_request)
            if child is None:
                # An already admitted parent cannot silently spawn an unowned
                # child if the new-admission switch changes during its pause.
                raise RunOwnershipConflict("The child execution cannot be admitted.")
            bind_operation_run(operation, child)
            dispatch = {"schema_version": 1, "client_request_id": key, "workflow": "scaffold_articles",
                "source_run_id": locked.run_id, WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY])}
            if BUDGET_KEY in child_request:
                dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
            children["dispatches"][key] = dispatch
            ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, CHILDREN_RESULT_KEY: children})
    payload.update({WIRE_REQUEST_KEY: parent_wire, WIRE_EXECUTION_KEY: parent_ticket, "child_dispatch_v1": dispatch})
    return child


def _prepare_legacy_scan_child_dispatch(*, run, user, actor_id, payload):
    """Admit only a new child; the actual legacy source remains unmarked."""
    from founder_tools import startup_team
    from integrations.services.article_generation import ArticleGenerationError, _validate_authenticated_content_factory_actor
    from workflow_runs.models import ContentFactoryRun
    from .run_legacy_source import fetch_legacy_source, validate_legacy_source
    from .website_connections import authority_guard, dispatch_contract
    from .website_operations import reserve_workflow_operation, bind_operation_run
    if (run.organization_id is None or not run.github_repo or user is None or not user.is_active
            or run.workflow not in {"scan", "repo_scan", "content_factory_scan"}):
        raise RunOwnershipConflict("Run a fresh website scan before approving this setup.")
    if "child_dispatch_v1" in payload:
        raise RunOwnershipConflict("A caller cannot supply a child execution identity.")
    try:
        _validate_authenticated_content_factory_actor(user=user, actor_id=actor_id)
    except (ArticleGenerationError, ValueError) as exc:
        raise RunOwnershipConflict("The current setup requester changed.") from exc
    history = (run.result or {}).get(CHILDREN_RESULT_KEY, {})
    dispatches = history.get("dispatches", {}) if isinstance(history, dict) else None
    if not isinstance(dispatches, dict):
        raise RunOwnershipConflict("The original setup dispatch could not be verified.")
    prior = [item for item in dispatches.values() if isinstance(item, dict)
             and item.get("workflow") == "scaffold_articles" and item.get("source_run_id") == run.run_id
             and "legacy_source_v1" in item]
    if len(prior) > 1:
        raise RunOwnershipConflict("The original setup dispatch is ambiguous. Run a fresh website scan.")
    # A retry keeps the original descriptor and ticket even after new admission
    # is switched off or the saved child's own execution has advanced.
    descriptor = (validate_legacy_source(run, prior[0]["legacy_source_v1"]) if prior else fetch_legacy_source(run))
    binding = dispatch_contract(run.domain, {}, action="setup", source_run_id=run.run_id)
    binding.update(domain=run.domain, github_repo=run.github_repo)
    with authority_guard(binding, action="setup", require_current_head=True) as connection:
        with startup_team._locked(run.organization) as (_, roster):
            if roster and roster.get("frozen") or startup_team._owner(run.organization) != user.pk:
                raise RunOwnershipConflict("The current startup owner must approve this setup.")
            locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            if (owner_receipt(locked) is not None or locked.run_id != run.run_id
                    or locked.organization_id != connection.organization_id or locked.domain != run.domain
                    or locked.github_repo != run.github_repo or locked.workflow != run.workflow
                    or locked.status in {"cancelled", "denied", "failed", "blocked"}):
                raise RunOwnershipConflict("The original scan changed before child admission.")
            children = deepcopy((locked.result or {}).get(CHILDREN_RESULT_KEY,
                {"schema_version": 1, "dispatches": {}}))
            if (not isinstance(children, dict) or children.get("schema_version") != 1
                    or not isinstance(children.get("dispatches"), dict)):
                raise RunOwnershipConflict("The original setup dispatch could not be verified.")
            key = "r3-scan-child:" + evidence_digest({"legacy_source_v1": descriptor, "workflow": "scaffold_articles"})
            previous = children["dispatches"].get(key)
            if previous is not None:
                child = admitted_run_for_key(key, lock=True)
                child_owner = owner_receipt(child)
                ticket = previous.get(WIRE_EXECUTION_KEY) if isinstance(previous, dict) else None
                if (child_owner is None or not isinstance(ticket, dict)
                        or previous.get("legacy_source_v1") != descriptor
                        or previous.get("client_request_id") != key or previous.get("workflow") != "scaffold_articles"
                        or previous.get("source_run_id") != locked.run_id
                        or previous.get(WIRE_REQUEST_KEY) != wire_identity(child_owner)
                        or child_owner["identity"]["principal"] != user_principal(user.pk, actor_id)
                        or child.organization_id != locked.organization_id or child.domain != locked.domain
                        or child.github_repo != locked.github_repo or child.workflow != "scaffold_articles"
                        or child.status in {"cancelled", "denied"} or child.run_request.get("legacy_source_v1") != descriptor
                        or child.run_request.get("parent_run_id") != locked.run_id
                        or previous.get(BUDGET_KEY) != child.run_request.get(BUDGET_KEY)):
                    raise RunOwnershipConflict("The original setup dispatch changed.")
                require_event_execution(child, previous, generation=ticket.get("generation"))
                payload["child_dispatch_v1"] = deepcopy(previous)
                return child
            if prior or not admission_enabled():
                raise RunOwnershipConflict("The original setup dispatch is unavailable. Run a fresh scan.")
            child_request = {key: value for key, value in binding.items()
                             if key in {*CONNECTION_FIELDS, "expected_source_sha"}}
            child_request.update(domain=locked.domain, github_repo=locked.github_repo, client_request_id=key,
                parent_run_id=locked.run_id, source_run_id=locked.run_id, scan_run_id=locked.run_id,
                legacy_source_v1=deepcopy(descriptor))
            operation = reserve_workflow_operation(connection, workflow="scaffold_articles", payload=child_request)
            child = reserve_admitted_run(organization=locked.organization, principal=user_principal(user.pk, actor_id),
                workflow="scaffold_articles", payload=child_request)
            if child is None:
                raise RunOwnershipConflict("The new setup child cannot be admitted.")
            bind_operation_run(operation, child)
            dispatch = {"schema_version": 1, "workflow": "scaffold_articles", "client_request_id": key,
                "source_run_id": locked.run_id, "legacy_source_v1": deepcopy(descriptor),
                WIRE_REQUEST_KEY: deepcopy(child_request[WIRE_REQUEST_KEY]),
                WIRE_EXECUTION_KEY: deepcopy(child_request[WIRE_EXECUTION_KEY])}
            if BUDGET_KEY in child_request:
                dispatch[BUDGET_KEY] = deepcopy(child_request[BUDGET_KEY])
            children["dispatches"][key] = dispatch
            ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**locked.result, CHILDREN_RESULT_KEY: children})
    payload["child_dispatch_v1"] = dispatch
    return child


def accept_scan_child_response(child, data, *, status_code=202):
    """Bind only the independently returned setup child, never its parent ID."""
    if child is None:
        return None
    from .dispatch_admission import pending_response, resolve_dispatch_outcome
    from .run_ownership import bind_admitted_run
    if not isinstance(data, dict):
        return resolve_dispatch_outcome(child)
    expected = owner_receipt(child)["identity"]["dispatch_key"]
    if data.get("child_client_request_id") not in (None, "", expected):
        return pending_response(child)
    if 200 <= status_code < 300:
        remote_id = data.get("scaffold_job_id") or data.get("child_run_id")
        if remote_id:
            return bind_admitted_run(child, remote_id)
        return resolve_dispatch_outcome(child)
    if status_code >= 500:
        return resolve_dispatch_outcome(child)
    return None
