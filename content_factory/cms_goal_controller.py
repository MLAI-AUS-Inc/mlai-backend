"""Per-site native controller over original capture, paid run and CMS journals."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_budget as native_budget
from . import cms_goal_contract as goals
from . import cms_goal_dispatch as dispatch
from . import cms_goal_recovery as recovery
from . import cms_goal_evidence as evidence
from . import cms_goal_refunds as refunds
from . import website_goal_budget as budget
from . import cms_capture as capture
from . import cms_artifact_review as reviews
from . import cms_owned_media as media
from . import cms_staged_review as stages
from . import cms_article_approval as approvals
from . import cms_article_publication as publications
from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import classify_failure, validate_failure, FailureContractError

LEASE_SECONDS = 180


def _save(operation, lease, *, now, state="pending", status, step=None, payload=None, receipt=None, delay=15):
    """Persist only the original current lease under the owning organization lock."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation.pk, action=authority.ACTION,
            state="running", payload__lease_id=lease).first()
        if saved is None:
            return None
        value = authority.current(saved, lease_id=lease)
        selected = native_budget.validate(saved.receipt.get("native_budget"), value.goal, now=now)
        updates = deepcopy(receipt or {})
        if state == "needs_user" and (updates.get("user_action") or {}).get("id") == "review_draft":
            choice = WebsiteConnectionOperation.objects.filter(action=approvals.ACTION,
                payload__stage_operation_id=updates["user_action"]["stageId"],
                payload__staged_revision=updates["user_action"]["reviewRevision"]).first()
            if choice is not None:
                approvals._current(choice)
                state, status, delay = "pending", "approval_pending", 0
                updates.pop("user_action", None)
        selected = updates.get("native_budget", selected)
        selected = budget.update_owner_wait(selected, waiting=state == "needs_user" and bool(updates.get("user_action")), now=now)
        saved.payload = {**deepcopy(saved.payload), **deepcopy(payload or {})}
        saved.payload.pop("lease_id", None)
        saved.receipt = {**deepcopy(saved.receipt), **updates, "native_budget": selected, "status": status}
        if step is not None: saved.receipt["current_step"] = step
        if state != "needs_user": saved.receipt.pop("user_action", None)
        saved.state = state
        saved.next_attempt_at = now + timedelta(seconds=delay) if state == "pending" else now + timedelta(minutes=5) if state == "needs_user" else None
        saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
        if state in refunds.billing.records.REFUND_TERMINAL:
            refunds.reserve_refund(saved, now=now)
        return saved


def _failure(operation, lease, failure, *, occurrence, now, child=None, progress=None, payload=None, run=None):
    """Account a real occurrence once; polling the same child never burns retries."""
    value = authority.current(operation, lease_id=lease)
    progress = deepcopy(progress or {})
    accounting = native_budget.validate(progress.get("native_budget", operation.receipt.get("native_budget")), value.goal, now=now)
    failure = validate_failure(failure)
    accounting, effective, fresh = budget.record_policy_occurrence(accounting, occurrence=occurrence,
        fingerprint=failure["failure_fingerprint"], classification=failure["failure_class"])
    if fresh:
        accounting["attempts_by_class"][failure["failure_class"]] += 1
    reason = budget.exhaustion(accounting, now=now)
    if reason is not None:
        return _save(operation, lease, now=now, state="stopped", status="budget_stopped", payload=payload,
            receipt={**progress, "budget_stop_reason": reason, "native_budget": accounting})
    if run is not None and effective in {"transient", "repairable"} and recovery.enabled(operation):
        updates = {**progress, "failure": failure, "failure_occurrence_id": occurrence,
            "effective_failure_class": effective, "native_budget": accounting}
        try:
            recovery.locator(operation, run)
            # Persist this occurrence and its known usage before reserving a
            # repair. The next lease can require the updated original budget.
            if not fresh:
                recovery.prepare(authority.CmsGoalDispatch(str(operation.pk), lease), now=now)
        except WebsiteAuthorityError as exc:
            return _save(operation, lease, now=now, state="needs_attention", status="native_recovery_blocked",
                payload=payload, receipt={**updates, "recovery_block_reason": exc.code})
        return _save(operation, lease, now=now, status="native_recovery_pending", step="first_draft",
            payload=payload, receipt=updates, delay=0)
    terminal = effective in {"strategy", "unsupported", "repairable"}
    state = "needs_attention" if terminal else "needs_user" if effective == "needs_user" else "pending"
    if fresh and effective == "transient" and child is not None:
        _rearm(operation, lease, child, now=now)
    if state == "needs_user": progress["user_action"] = {"id": "connect_cms"}
    # Native repair/strategy must keep this original paid run and guard unknown
    # posts. Never substitute a second generation or fabricate a Git rung.
    return _save(operation, lease, now=now, state=state,
        status="native_recovery_required" if terminal else "owner_action_required" if state == "needs_user" else "retrying",
        payload=payload, receipt={**progress, "failure": failure, "failure_occurrence_id": occurrence, "effective_failure_class": effective,
            "native_budget": accounting}, delay=60 if effective == "transient" else 15)


def _rearm(operation, lease, child, *, now):
    """Retry the original read/phase journal without restoring a write allowance."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
        parent = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation.pk, action=authority.ACTION,
            state="running", payload__lease_id=lease).first()
        if parent is None: return False
        authority.current(parent, lease_id=lease)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=child.pk,
            connection_id=parent.connection_id, generation=parent.generation, state="blocked", updated_at=child.updated_at).first()
        if saved is None: return False
        payload = deepcopy(saved.payload)
        payload.pop("lease_id", None)
        if "failures" in payload: payload["failures"] = 0
        if "page_failures" in payload: payload["page_failures"] = 0
        saved.payload, saved.state, saved.next_attempt_at = payload, "pending", now
        saved.save(update_fields=["payload", "state", "next_attempt_at", "updated_at"])
        return True


def _problem(child):
    if child is None or child.state not in {"blocked", "needs_user", "cancelled", "needs_attention", "deleted"}:
        return None
    try:
        failure = validate_failure(child.receipt.get("failure"))
    except FailureContractError:
        failure = classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_goal_child")
    occurrence = digest({"operation": str(child.pk), "updated_at": child.updated_at.isoformat(), "state": child.state,
        "fingerprint": failure["failure_fingerprint"]})
    return failure, occurrence


def _pipeline(run, *, now):
    """Reserve only this original run's journal chain; all provider HTTP is scheduled."""
    from .website_models import WebsiteConnectionOperation
    review = reviews.start_review(run)
    chain = {reviews.ACTION: review}
    if review.state != "completed": return chain
    owned = media.start_media(review)
    chain[media.ACTION] = owned
    if owned.state != "completed": return chain
    stage = stages.start_stage(owned)
    chain[stages.ACTION] = stage
    approval = WebsiteConnectionOperation.objects.filter(action=approvals.ACTION, payload__stage_operation_id=str(stage.pk),
        payload__staged_revision=stage.payload["staged"]["revision"]).first()
    if approval is None and stage.payload["review_policy"] == "auto":
        approval = approvals._start(stage, basis="owner_setup_auto", expected_revision=stage.payload["staged"]["revision"])
    if approval is None: return chain
    chain[approvals.ACTION] = approval
    if approval.state != "completed": return chain
    publication = WebsiteConnectionOperation.objects.filter(action=publications.ACTION, payload__approval_operation_id=str(approval.pk)).first()
    if publication is None: publication = publications.start_publication(approval)
    chain[publications.ACTION] = publication
    if publication.state == "completed": publications.request_public_recheck(publication, now=now)
    return chain


def _account(operation, run, *, now):
    selected = native_budget.validate(operation.receipt.get("native_budget"), operation.receipt["goal"], now=now)
    if run is None: return selected
    report = run.result.get("usage") if isinstance(run.result, dict) else None
    if report is None:
        if run.status in {"completed", "failed", "blocked", "cancelled", "denied"}:
            selected["missing_usage_run_ids"] = [run.run_id]
        return selected
    try:
        if (report.get("generation") != run.result.get("generation") or
                run.result.get("state_version") is not None and report.get("state_version") != run.result["state_version"]):
            raise ValueError("Native usage does not match its current original run snapshot")
        selected = budget.account_usage(selected, report, run_id=run.run_id)
        selected["missing_usage_run_ids"] = []
    except ValueError:
        selected["usage_invalid"] = True
    return selected


def _advance(operation, lease, *, now):
    from .website_models import WebsiteConnectionOperation
    value = authority.current(operation, lease_id=lease)
    goal = value.goal
    # A previously bound run is never forgotten or replaced by an absent mirror.
    original = dispatch.original_dispatch(operation)
    run = dispatch.original_run(operation, original) if original is not None else None
    accounting = _account(operation, run, now=now)
    reason = budget.exhaustion(accounting, now=now)
    if reason is not None:
        return _save(operation, lease, now=now, state="stopped", status="budget_stopped", receipt={"budget_stop_reason": reason, "native_budget": accounting})
    plan = operation.receipt.get("native_recovery")
    if plan is not None:
        plan = recovery.validate_plan(plan, operation)
        if plan["state"] in recovery.ACTIVE:
            if not recovery.enabled(operation):
                return _save(operation, lease, now=now, status="native_recovery_paused", receipt={"native_budget": accounting}, delay=300)
            scope = authority.CmsGoalDispatch(str(operation.pk), lease)
            if not recovery.observe_progress(scope, now=now):
                if plan["state"] == "reserved":
                    try:
                        from .vibe_marketing_views import _control_article_run_for_channel
                        _control_article_run_for_channel(user=value.user, context=value.context, run=run,
                            action="resume", data={}, cms_goal_scope=scope)
                    except WebsiteAuthorityError as exc:
                        return _save(operation, lease, now=now, state="pending" if exc.retryable else "needs_attention",
                            status="native_recovery_pending" if exc.retryable else "native_recovery_blocked",
                            receipt={"native_budget": accounting, **({} if exc.retryable else {"recovery_block_reason": exc.code})}, delay=30)
                return _save(operation, lease, now=now, status="native_recovery_pending", receipt={"native_budget": accounting}, delay=15)
            operation.refresh_from_db()
            run.refresh_from_db()
            value = authority.current(operation, lease_id=lease)
            goal = value.goal
            accounting = _account(operation, run, now=now)
            reason = budget.exhaustion(accounting, now=now)
            if reason is not None:
                return _save(operation, lease, now=now, state="stopped", status="budget_stopped",
                    receipt={"budget_stop_reason": reason, "native_budget": accounting})
    capture_id = operation.payload["capture_operation_id"]
    inventory = WebsiteConnectionOperation.objects.filter(pk=capture_id, action=capture.ACTION).first() if capture_id else None
    if inventory is None:
        if capture_id is not None:
            raise WebsiteAuthorityError("website_operation_changed", "The original native inventory reservation is missing.")
        from .website_connections import contract_for
        inventory = capture.start_capture(value.context, value.config, user=value.user,
            data={**contract_for(value.connection), "client_request_id": str(operation.pk)})
        return _save(operation, lease, now=now, status="capturing_existing", step="capture_existing",
            payload={"capture_operation_id": str(inventory.pk)}, receipt={"native_budget": accounting}, delay=0)
    if (inventory.payload.get("binding") != value.scope["binding"] or
            inventory.payload.get("credential_operation_id") != operation.payload["credential_operation_id"]):
        raise WebsiteAuthorityError("website_operation_changed", "The original native inventory scope changed.")
    problem = _problem(inventory)
    if problem: return _failure(operation, lease, problem[0], occurrence=problem[1], now=now, child=inventory)
    if inventory.state != "completed":
        return _save(operation, lease, now=now, status="capturing_existing", step="capture_existing", receipt={"native_budget": accounting})
    # Setup proofs validate complete inventory, never a partial list or count.
    setup_goal = goals.new_goal(goal["goal_id"], value.scope, now=goals._time(goal["started_at"]))
    setup, _source, _refs = evidence.collect(setup_goal, value.context, value.config, user=value.user, capture_operation_id=str(inventory.pk), now=now)
    if "understood" not in setup:
        failure = classify_failure("SOURCE_CAPTURE_INVALID", "cms_goal_inventory")
        return _failure(operation, lease, failure, occurrence="inventory:" + str(inventory.pk), now=now)
    if operation.payload["generation_data"] is None:
        from .website_first_article_topics import current_selection
        choice = current_selection(value.context, value.config)
        return _save(operation, lease, now=now, status="topic_selected", step="first_draft", payload={"generation_data": choice},
            receipt={"goal": goals.refresh(goal, setup, expected_scope=value.scope, now=now), "native_budget": accounting}, delay=0)
    run = dispatch.advance(operation, lease_id=lease, now=now)
    if run is None:
        return _save(operation, lease, now=now, status="dispatching_original", step="first_draft", receipt={"native_budget": accounting})
    original = dispatch.original_dispatch(operation)
    request = original.payload["request"]
    artifact_id = request["publishing_channel_artifact"]["artifact_operation_id"]
    actual_run = run if isinstance(run.result, dict) and type(run.result.get("generation")) is int and run.run_id != original.client_request_id else None
    chain = _pipeline(run, now=now) if run.status in {"completed", "awaiting_approval", "approval_required"} else {}
    if run.status in {"failed", "blocked", "cancelled", "denied"}:
        try: failure = validate_failure(run.result.get("failure"))
        except (FailureContractError, AttributeError): failure = classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_goal_generation")
        occurrence = run.result.get("failure_occurrence_id") or digest({"run": run.run_id,
            "generation": run.result.get("generation"), "failure": failure["failure_fingerprint"]})
        return _failure(operation, lease, failure, occurrence=occurrence, now=now, run=run,
            progress={"native_budget": accounting}, payload={"source_run_id": run.run_id})
    proofs, actual_source, references = evidence.collect(goal, value.context, value.config, user=value.user,
        run=actual_run, artifact_operation_id=artifact_id, capture_operation_id=str(inventory.pk), now=now)
    if actual_source is not None: goal = goals.bind_source(goal, actual_source, expected_scope=value.scope, now=now)
    goal = goals.refresh(goal, proofs, expected_scope=value.scope, now=now)
    changes = {"goal": goal, "native_budget": _account(operation, run, now=now), "references": references}
    if goals.complete(goal, expected_scope=value.scope, now=now):
        publication = chain[publications.ACTION]
        changes["public_url"] = publications.current_publication(publication, now=now)[3]["public_url"]
        return _save(operation, lease, now=now, state="completed", status="first_article_live", step="first_article_live",
            payload={"source_run_id": run.run_id}, receipt=changes)
    for child in chain.values():
        problem = _problem(child)
        if problem: return _failure(operation, lease, problem[0], occurrence=problem[1], now=now, child=child,
            progress=changes, payload={"source_run_id": run.run_id})
    stage = chain.get(stages.ACTION)
    if stage is not None and stage.payload["review_policy"] == "review" and approvals.ACTION not in chain:
        changes["user_action"] = {"id": "review_draft", "runId": run.run_id, "stageId": str(stage.pk),
            "reviewRevision": stage.payload["staged"]["revision"]}
        return _save(operation, lease, now=now, state="needs_user", status="review_required", step="approved",
            payload={"source_run_id": run.run_id}, receipt=changes)
    step = "first_article_live" if publications.ACTION in chain else "approved" if approvals.ACTION in chain else "previewed" if media.ACTION in chain else "first_draft"
    return _save(operation, lease, now=now, status="publishing" if publications.ACTION in chain else "working", step=step,
        payload={"source_run_id": run.run_id}, receipt=changes)


def _authority_lost(candidate, lease, *, now, claimed, error):
    """Fence stale scope without leaving a permanently due goal or losing money."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=candidate.pk, action=authority.ACTION, **owned).first()
        if saved is None: return None
        saved.payload = deepcopy(saved.payload)
        saved.payload.pop("lease_id", None)
        if not authority.enabled(saved.connection.organization_id):
            saved.state, saved.next_attempt_at = "pending", now
            saved.save(update_fields=["payload", "state", "next_attempt_at", "updated_at"])
            return saved
        try:
            value = authority.historical(saved)
        except WebsiteAuthorityError:
            saved.state, saved.next_attempt_at = "needs_attention", None
            saved.receipt = {**saved.receipt, "status": "invalid_native_goal"}
            saved.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
            return saved
        needs_owner = error.authority_code == "missing_or_expired_credentials"
        saved.state, saved.next_attempt_at = ("needs_user", None) if needs_owner else ("cancelled", None)
        saved.receipt = {**saved.receipt, "status": "credentials_required" if needs_owner else "authority_changed"}
        if needs_owner:
            selected = native_budget.validate(saved.receipt.get("native_budget"), value.goal, now=now)
            saved.receipt.update(native_budget=budget.update_owner_wait(selected, waiting=True, now=now),
                user_action={"id": "connect_cms"})
        else:
            saved.receipt.pop("user_action", None)
        saved.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
        if not needs_owner: refunds.reserve_refund(saved, now=now)
        return saved


def advance_goal(identifier, *, now=None):
    """Claim one native goal; provider jobs, reviews and callbacks wake later leases."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None or not authority.enabled(candidate.connection.organization_id): return None
    now, lease, claimed = now or timezone.now(), str(uuid4()), False
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
            if operation is None or operation.state not in authority.ACTIVE or operation.next_attempt_at and operation.next_attempt_at > now:
                return operation
            authority.current(operation)
            operation.payload = {**deepcopy(operation.payload), "lease_id": lease}
            operation.state, operation.next_attempt_at = "running", now + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
        claimed = True
        return _advance(operation, lease, now=now)
    except WebsiteAuthorityError as exc:
        if not claimed: return _authority_lost(candidate, lease, now=now, claimed=False, error=exc)
        if (isinstance(getattr(exc, "result", None), dict) and exc.result.get("field") == "topicCandidateId" and
                operation.payload["failures"] < 2 and dispatch.original_dispatch(operation) is None):
            return _save(operation, lease, now=now, status="selecting_next_topic", step="first_draft",
                payload={"generation_data": None, "failures": operation.payload["failures"] + 1}, delay=0)
        try:
            failure = validate_failure(getattr(exc, "result", {}).get("failure"))
        except (FailureContractError, AttributeError):
            failure = exc.failure or classify_failure("CONNECTION_SERVICE_TRANSIENT" if exc.retryable else "PUBLISHING_ADAPTER_REQUIRED", "cms_goal_controller")
        try:
            return _failure(operation, lease, failure, occurrence="controller:" + str(operation.attempts) + ":" + failure["failure_fingerprint"], now=now)
        except WebsiteAuthorityError as changed:
            # A newer owner/lifecycle action wins; never restore an old lease.
            return _authority_lost(operation, lease, now=now, claimed=True, error=changed)
    except (CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        if not claimed: return candidate
        failure = classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_goal_contract")
        try:
            return _failure(operation, lease, failure, occurrence="contract:" + str(operation.attempts), now=now)
        except WebsiteAuthorityError as changed:
            return _authority_lost(operation, lease, now=now, claimed=True, error=changed)


def wake_for_approval(stage, source):
    """Wake the exact waiting goal while the owner's approval holds the org lock."""
    if not authority.enabled(source.run.organization_id): return
    from .website_models import WebsiteConnectionOperation
    operation = WebsiteConnectionOperation.objects.select_for_update().filter(action=authority.ACTION,
        connection_id=stage.connection_id, generation=stage.generation, state="needs_user",
        payload__source_run_id=source.run.run_id, receipt__user_action__stageId=str(stage.pk),
        receipt__user_action__reviewRevision=stage.payload["staged"]["revision"]).first()
    if operation is None: return
    value = authority.current(operation)
    selected = native_budget.validate(operation.receipt.get("native_budget"), value.goal, now=timezone.now())
    selected = budget.update_owner_wait(selected, waiting=False, now=timezone.now())
    operation.state, operation.next_attempt_at = "pending", timezone.now()
    operation.receipt = {**operation.receipt, "status": "approval_pending", "native_budget": selected}
    operation.receipt.pop("user_action", None)
    operation.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
