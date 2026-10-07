"""Versioned operation fences using the existing durable operation envelope."""

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .website_contract import WebsiteAuthorityError, connection_contract, evidence_digest
from .website_models import WebsiteConnectionOperation


OPERATION_FIELDS = ("operation_id", "operation_attempt", "deletion_epoch")


def deletion_epoch(connection):
    """Return the retained erasure watermark; reconnect never resets it."""
    return max((int(row.get("deletion_epoch", 0)) for row in (connection.blockers or []) if isinstance(row, dict)), default=0)


def validate_operation(connection, payload, *, worker_cleanup=False, restoration=False, cancellation=False):
    """Deny cancelled/deleted work, including legacy callbacks with a run ID."""
    from workflow_runs.models import ContentFactoryRun
    run_id = str(payload.get("run_id") or payload.get("job_id") or "")
    if run_id and not worker_cleanup and not restoration and not cancellation:
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run and (run.organization_id != connection.organization_id or run.status in {"cancelled", "denied"}):
            raise WebsiteAuthorityError("website_operation_cancelled", "This operation was cancelled or belongs to another company.")
        saved = (run.run_request or {}) if run else {}
        for key in OPERATION_FIELDS:
            if key in saved and key in payload and str(saved[key]) != str(payload[key]):
                raise WebsiteAuthorityError("website_operation_changed", "The operation identity changed.")
        payload = {**saved, **payload}
    identifier = payload.get("operation_id")
    if not identifier:
        if worker_cleanup or restoration or cancellation or (deletion_epoch(connection) and run_id):
            raise WebsiteAuthorityError("website_operation_required", "A current operation identity is required after removal.")
        return None
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_operation_id", "A valid operation ID is required.") from exc
    op = connection.operations.filter(pk=identifier).first()
    if op is None:
        raise WebsiteAuthorityError("website_operation_changed", "The operation does not belong to this website.")
    try:
        from .website_contract import positive_integer
        attempt = positive_integer(payload.get("operation_attempt", 1), name="operation_attempt")
        raw_epoch = payload.get("deletion_epoch", 0)
        if isinstance(raw_epoch, bool) or not str(raw_epoch).isdigit():
            raise ValueError("invalid deletion watermark")
        epoch = int(raw_epoch)
    except (ValueError, TypeError) as exc:
        raise WebsiteAuthorityError("website_operation_changed", "The operation attempt and deletion watermark must be integers.") from exc
    if attempt < 1 or attempt != op.payload.get("attempt", 1) or epoch != deletion_epoch(connection):
        raise WebsiteAuthorityError("website_operation_changed", "The operation attempt or deletion watermark changed.")
    if op.state in {"cancelled", "deleted", "denied"} and not cancellation:
        raise WebsiteAuthorityError("website_operation_cancelled", "This operation was cancelled.")
    if op.state in {"completed", "failed", "blocked"} and not (worker_cleanup or restoration or cancellation):
        incoming_status = payload.get("status")
        event = payload.get("event_type") or payload.get("event")
        terminal_events = {
            "completed": {"article_complete", "generation_pr_opened", "publish_bundle_ready", "scan_complete", "article_system_setup_complete", "article_system_setup_completed", "scaffold_complete", "article_review_ready", "content_ready"},
            "failed": {"generation_failed", "error", "article_system_setup_failed"},
            "blocked": {"generation_blocked"},
        }
        if (incoming_status and incoming_status != op.state) or (event and event not in terminal_events[op.state]):
            raise WebsiteAuthorityError("website_operation_terminal", "This operation is terminal. Start a new reviewed attempt.")
    if cancellation:
        if (op.action != "workflow" or op.state != "cancelled" or op.generation != connection.generation
                or str(payload.get("connection_generation")) != str(connection.generation) or not run_id or run_id != op.payload.get("run_id")):
            raise WebsiteAuthorityError("cancellation_scope_mismatch", "Cancellation is limited to the already-fenced operation.")
    elif worker_cleanup:
        allowed = op.payload.get("purge_run_ids", op.payload.get("stop_preview_run_ids", []))
        requested = payload.get("run_ids", [])
        if isinstance(requested, str):
            import json
            try:
                requested = json.loads(requested)
            except ValueError:
                requested = [requested]
        if (op.action != "purge" or op.generation != connection.generation
                or int(payload.get("connection_generation", 0)) != op.payload.get("previous_generation")
                or not isinstance(requested, list) or not set(requested).issubset(set(allowed))):
            raise WebsiteAuthorityError("worker_cleanup_scope_mismatch", "Cleanup is limited to the approved removal manifest.")
    elif restoration:
        if (op.action != "cleanup" or op.generation != connection.generation
                or payload.get("setup_run_id") not in op.payload.get("setup_run_ids", [])):
            raise WebsiteAuthorityError("restoration_scope_mismatch", "Restoration is limited to the owned integration manifest.")
        if payload.get("action") == "restoration":
            approved = op.payload.get("approved_restoration") or {}
            if not isinstance(approved, dict) or not approved or payload.get("plan_digest") != approved.get("plan_digest") or payload.get("expected_base_sha") != approved.get("expected_base_sha"):
                raise WebsiteAuthorityError("restoration_approval_required", "Review and approve this exact inverse patch first.")
    elif op.generation != connection.generation:
        raise WebsiteAuthorityError("website_operation_changed", "The operation belongs to a previous connection generation.")
    return op


def reserve_workflow_operation(connection, *, workflow, payload):
    """Reserve a stable operation before any dispatch, preserving debit identity."""
    from .website_connections import authority_guard
    # Request keys survive reload in the operation row. An in-flight logical
    # scan/setup also resumes when an older client minted a replacement key.
    digest = evidence_digest({key: value for key, value in payload.items() if key not in {
        "client_request_id", "idempotency_key", "roo_points_gate", "roo_points_authorized", "operation_id", "operation_attempt", "deletion_epoch"}})
    key = f"{connection.pk}:workflow:{payload['client_request_id']}"
    with authority_guard(payload, action="read"):
        active = connection.operations.filter(generation=connection.generation, action="workflow", state__in=["running", "pending"],
            payload__request_digest=digest).first() if workflow in {"repo_scan", "content_factory_scan", "article_system_setup"} else None
        if active:
            key = active.idempotency_key
            payload["client_request_id"] = active.payload["client_request_id"]
        op, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key,
            defaults={"connection": connection, "generation": connection.generation, "action": "workflow", "state": "running",
                "payload": {"workflow": workflow, "attempt": 1, "deletion_epoch": deletion_epoch(connection), "request_digest": digest,
                    "client_request_id": payload["client_request_id"]}})
        if (op.generation != connection.generation or op.payload.get("workflow") != workflow
                or op.payload.get("request_digest") != digest or op.state in {"cancelled", "deleted", "denied"}):
            raise WebsiteAuthorityError("operation_key_conflict", "This request identity belongs to different or cancelled work.")
        payload.update(operation_id=str(op.pk), operation_attempt=op.payload.get("attempt", 1), deletion_epoch=deletion_epoch(connection))
        from .website_connections import extend_owner_operation_contract
        extend_owner_operation_contract({key: payload[key] for key in OPERATION_FIELDS})
    return op


def bind_operation_run(operation, run):
    """Bind a returned run only if cancellation has not superseded dispatch."""
    from .website_connections import authority_guard, contract_for
    binding = {**contract_for(operation.connection), "operation_id": str(operation.pk), "operation_attempt": operation.payload.get("attempt", 1),
        "deletion_epoch": operation.payload.get("deletion_epoch", 0)}
    try:
        with authority_guard(binding, action="read"):
            operation.refresh_from_db()
            operation.payload = {**operation.payload, "run_id": run.run_id}
            operation.state = "completed" if run.status == "completed" else "failed" if run.status in {"failed", "blocked"} else "running"
            operation.receipt = {"status": operation.state, "run_id": run.run_id, "repository_modified": None, "remote_outcome_unknown": True}
            operation.save(update_fields=["payload", "state", "receipt", "updated_at"])
    except WebsiteAuthorityError:
        # An ambiguous response can arrive after cancellation. Retain only the
        # remote reference for cleanup; never resurrect the operation or run.
        from organizations.models import Organization
        from .website_models import WebsiteConnection
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
            website = WebsiteConnection.objects.select_for_update().get(pk=operation.connection_id)
            op = WebsiteConnectionOperation.objects.select_for_update().get(pk=operation.pk)
            op.payload = {**op.payload, "run_id": run.run_id}
            op.state = "cancelled"
            op.save(update_fields=["payload", "state", "updated_at"])
            cleanup, _ = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{website.pk}:late-dispatch:{op.pk}", defaults={
                "connection": website, "generation": website.generation, "action": "cancel-operation" if op.generation == website.generation else "disconnect",
                "payload": {"previous_generation": op.generation, "cancelled_operation_id": str(op.pk), "cancel_run_ids": [run.run_id], "stop_preview_run_ids": [run.run_id]},
                "receipt": {"status": "late_dispatch_cancel_requested", "remote_cleanup_pending": True, "repository_modified": None}})
            run.status, run.resume_available, run.error = "cancelled", False, "Website authority changed during dispatch."
            run.save(update_fields=["status", "resume_available", "error", "updated_at"])


def operation_summary(op):
    """Public receipt without internal provider credentials or source bodies."""
    state = op.state
    if op.action == "workflow" and state == "running" and op.payload.get("run_id"):
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.filter(run_id=op.payload["run_id"]).first()
        if run and run.status in {"completed", "failed", "cancelled", "denied", "blocked"}:
            state = "failed" if run.status == "blocked" else run.status
    return {"id": str(op.pk), "action": op.action, "state": state, "attempt": op.payload.get("attempt", 1),
        "runId": op.payload.get("run_id"), "updatedAt": op.updated_at.isoformat(), "receipt": op.receipt}


def cancel_operation(config, *, data, idempotency_key):
    """Fence exactly one operation before remote cancellation is reconciled."""
    from .website_connections import authority_guard, contract_for
    from workflow_runs.models import ContentFactoryRun
    try:
        identifier = UUID(str(data.get("operation_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_operation_id", "Select a valid website operation.", status=422) from exc
    binding = {**connection_contract(data), "domain": config.organization.domain, "github_repo": config.github_repo}
    with authority_guard(binding, action="read") as connection:
        op = connection.operations.select_for_update().filter(pk=identifier, generation=connection.generation, action="workflow").first()
        if op is None:
            raise WebsiteAuthorityError("website_operation_not_found", "Select a current website operation.", status=404)
        from .website_contract import positive_integer
        attempt = positive_integer(data.get("operation_attempt", 1), name="operation_attempt")
        if attempt != op.payload.get("attempt", 1) or str(data.get("deletion_epoch", 0)) != str(deletion_epoch(connection)):
            raise WebsiteAuthorityError("website_operation_changed", "Refresh this operation before cancelling it.")
        if op.state in {"completed", "failed", "denied", "deleted"}:
            raise WebsiteAuthorityError("website_operation_terminal", "This operation has finished. Review its recorded effects or prepare a removal proposal.")
        run = ContentFactoryRun.objects.filter(run_id=op.payload.get("run_id"), organization=connection.organization).first()
        if run and run.status in {"completed", "failed", "denied", "blocked"}:
            raise WebsiteAuthorityError("website_operation_terminal", "The worker has finished this operation. Review its recorded effects.")
        effects = list(connection.repository_mutations.filter(run_id=op.payload.get("run_id", "")).values("pr_url", "branch", "head_sha", "status"))
        op.state = "cancelled"
        op.receipt = {**op.receipt, "status": "cancelled", "repository_modified": bool(effects) or None, "remote_effects": effects,
            "remote_outcome_unknown": not bool(effects), "cancellation_undoes_remote_writes": False, "remote_cleanup_pending": True}
        op.save(update_fields=["state", "receipt", "updated_at"])
        run_ids = list(ContentFactoryRun.objects.filter(organization=connection.organization, run_request__operation_id=str(op.pk)).values_list("run_id", flat=True))
        ContentFactoryRun.objects.filter(run_id__in=run_ids).update(status="cancelled", resume_available=False, error="Website operation cancelled.", updated_at=timezone.now())
        followup, _ = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{connection.pk}:cancel:{idempotency_key}", defaults={
            "connection": connection, "generation": connection.generation, "action": "cancel-operation", "payload": {"cancel_run_ids": run_ids,
                "stop_preview_run_ids": run_ids, "cancelled_operation_id": str(op.pk), "previous_generation": connection.generation},
            "receipt": dict(op.receipt)})
        return followup


def observe_workflow_status(run, payload=None):
    """Acknowledge an accepted worker observation for the current attempt only."""
    request = run.run_request or {}
    identifier = request.get("operation_id")
    if not identifier:
        return
    op = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action="workflow").first()
    if (op is None or op.generation != request.get("connection_generation")
            or str(op.connection_id) != request.get("website_connection_id")
            or op.payload.get("run_id") != run.run_id
            or op.payload.get("attempt", 1) != request.get("operation_attempt", 1)
            or op.state in {"cancelled", "deleted", "denied"}):
        return
    state = str(run.status)
    if op.payload.get("resume_pending") and state in {"failed", "blocked"}:
        from .run_state import execution_version
        incoming = payload if isinstance(payload, dict) else {}
        incoming_request = incoming.get("run_request") if isinstance(incoming.get("run_request"), dict) else {}
        attempt = incoming.get("operation_attempt", incoming_request.get("operation_attempt"))
        version = execution_version(run.result or {})
        baseline = op.payload.get("resume_execution_version")
        if attempt != op.payload.get("attempt") and (not version or not baseline or tuple(version) <= tuple(baseline)):
            return
    if state not in {"completed", "failed", "blocked"}:
        state = "running"
    if op.state in {"completed", "failed", "blocked"} and state != op.state:
        return
    op.state = state
    op.payload = {**op.payload, "resume_pending": False}
    op.receipt = {**(op.receipt or {}), "status": state, "run_id": run.run_id, "remote_outcome_unknown": False}
    op.save(update_fields=["state", "payload", "receipt", "updated_at"])


def advance_workflow_attempt(run):
    """Fence a resumed worker attempt before dispatch, retaining charge identity."""
    from .website_connections import authority_guard, scoped_run_contract, contract_for, extend_owner_operation_contract
    binding = scoped_run_contract(run)
    with authority_guard(binding, action="read") as website:
        identifier = binding.get("operation_id")
        if not identifier:
            # Older saved runs have consent identity but predate the operation
            # ledger. Upgrade only an eligible run under the current consent
            # lock, with a stable identity; never revive removed/finished work.
            if run.status not in {"failed", "blocked", "running", "queued"} or deletion_epoch(website):
                raise WebsiteAuthorityError("website_operation_required", "A current operation identity is required to resume this run.")
            from .website_models import WebsiteConnectionOperation
            legacy, _ = WebsiteConnectionOperation.objects.get_or_create(
                connection=website, generation=website.generation,
                idempotency_key=f"{website.pk}:legacy-resume:{run.pk}",
                defaults={"action": "workflow", "state": "failed",
                    "payload": {"workflow": run.workflow, "run_id": run.run_id, "attempt": 1, "deletion_epoch": 0}},
            )
            identifier = legacy.pk
        op = WebsiteConnectionOperation.objects.select_for_update().get(pk=identifier, connection=website)
        if op.state == "completed":
            raise WebsiteAuthorityError("website_operation_terminal", "Completed website work cannot be resumed.")
        if op.state not in {"failed", "blocked", "running", "pending"}:
            raise WebsiteAuthorityError("website_operation_cancelled", "This operation cannot be resumed.")
        previous_attempt = int(op.payload.get("attempt", 1))
        # Retransmission of an ambiguous resume uses the same reserved attempt.
        request_attempt = int((run.run_request or {}).get("operation_attempt", 1))
        if not (op.payload.get("resume_pending") and op.state in {"pending", "running"} and request_attempt == previous_attempt):
            from .run_state import execution_version
            op.payload = {**op.payload, "attempt": previous_attempt + 1, "resume_pending": True,
                "resume_execution_version": execution_version(run.result or {})}
        op.state = "running"
        op.save(update_fields=["payload", "state", "updated_at"])
        fields = {"operation_id": str(op.pk), "operation_attempt": op.payload["attempt"], "deletion_epoch": deletion_epoch(website)}
        run.run_request = {**(run.run_request or {}), **contract_for(website), **fields}
        run.save(update_fields=["run_request", "updated_at"])
        extend_owner_operation_contract(fields)
        return fields
