"""Fenced, state-only reception of the setup delivery protocol.

Network and business effects deliberately do not belong in this module. The
existing callback ledger and reserved run JSON hold transport receipts without
introducing a second delivery model or granting authority from worker input.
"""

from copy import copy, deepcopy
from contextlib import nullcontext
from datetime import timedelta
import logging
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.response import Response

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from .models import ContentFactoryCallbackEvent
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_contract import WebsiteAuthorityError, validate_authority

logger = logging.getLogger(__name__)
NAMESPACE = "delivery_v1"
TERMINAL = {"completed", "cancelled", "denied"}
LEASE_SECONDS = 120
RECEIPT_LIMIT = 2048
ARTICLE_WORKFLOWS = {"direct_generate","confirmed_topic"}


def apply_enabled(envelope):
    """Article and setup enrollment can be rolled back independently."""
    flag="CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED" if envelope["workflow"] in ARTICLE_WORKFLOWS else "CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED"
    return getattr(settings,flag,False)


class DeliveryReceiptConflict(ValueError):
    """Abort the entire claim transaction when an ID belongs to other bytes."""


class DeliveryScopeError(ValueError):
    """Transport disposition, deliberately independent of causal B1 failures."""
    def __init__(self, code):
        self.code, self.status, self.retryable = code, 409, False
        super().__init__(code)


def is_setup_delivery(payload):
    """Recognize versioned input before legacy guards can claim or bind it."""
    if not isinstance(payload, dict):
        return False
    kind = payload.get("kind")
    workflow = payload.get("workflow")
    event = payload.get("event_type")
    return (isinstance(kind, str) and kind in {"setup_callback", "run_snapshot"}
        or "schema_version" in payload and isinstance(workflow, str) and workflow in {"article_system_setup", "repo_scan"}
        or "schema_version" in payload and isinstance(event, str) and event.startswith(("article_system_setup_", "scan_"))
        or "schema_version" in payload and ("scope" in payload or "execution" in payload)
        or isinstance(payload.get("body"), dict) and "state_projection" in payload["body"])


def acknowledgement(envelope, disposition, *, code="", acknowledged=True, retryable=False):
    """Echo the immutable identity so a sender can verify an actual receipt."""
    receipt = {key: deepcopy(envelope[key]) for key in (
        "schema_version", "kind", "run_id", "event_id", "payload_sha256", "state_sha256", "scope")}
    return {**receipt, "status": disposition, "disposition": disposition, "code": code, "acknowledged": acknowledged,
            "retryable": retryable}


def _metadata(run):
    result = run.result if isinstance(run.result, dict) else {}
    value = result.get(NAMESPACE)
    return deepcopy(value) if isinstance(value, dict) else {}


def _save_metadata(run, metadata):
    run.result = {**(run.result or {}), NAMESPACE: deepcopy(metadata)}
    # Transport-only changes must not refresh the business staleness clock.
    run.save(update_fields=["result"])


def _original_scope_matches(run, scope):
    original = run.run_request if isinstance(run.run_request, dict) else {}
    if run.domain != scope["domain"] or run.github_repo != scope["github_repo"]:
        return False
    return all(original.get(key) == value for key, value in scope.items()
               if key not in {"domain", "github_repo"})


def _scope_error(code):
    raise DeliveryScopeError(code)


def _locked_context(envelope):
    """Acquire lifecycle locks in their established organization-first order."""
    scope = envelope["scope"]
    # Read only to select lock order; all identity is rechecked under locks.
    candidate = WebsiteConnection.objects.filter(pk=scope["website_connection_id"]).first()
    if candidate is None:
        _scope_error("website_connection_not_found")
    organization = Organization.objects.select_for_update().filter(pk=candidate.organization_id).first()
    website = WebsiteConnection.objects.select_for_update().select_related("organization").filter(pk=candidate.pk).first()
    if organization is None or website is None or website.organization_id != organization.pk:
        _scope_error("website_scope_mismatch")
    operation = WebsiteConnectionOperation.objects.select_for_update().filter(
        pk=scope["operation_id"], connection=website, action="workflow").first()
    if operation is None:
        _scope_error("website_operation_changed")
    article=envelope["workflow"] in ARTICLE_WORKFLOWS
    marker="article_delivery_v1" if article else "setup_delivery_v1"
    if (operation.payload or {}).get(marker) != {"schema_version": 1}:
        _scope_error("delivery_not_enrolled")
    if article:
        from .setup_delivery_enrollment import article_stream
        if not article_stream(operation.payload.get("article_delivery_request") or {}):
            _scope_error("delivery_not_enrolled")
    run = ContentFactoryRun.objects.select_for_update().filter(run_id=envelope["run_id"]).first()
    if run is not None:
        receipt = _metadata(run).get("receipts", {}).get(envelope.get("event_id"), {})
        historical_ack = (receipt.get("processed_at") and receipt.get("payload_sha256") == envelope.get("payload_sha256"))
        if run.organization_id != website.organization_id or not (_original_scope_matches(run, scope) or historical_ack):
            _scope_error("website_scope_mismatch")
        from .setup_delivery_enrollment import delivery_workflow,article_stream
        if delivery_workflow(run) != envelope["workflow"]:
            _scope_error("website_run_changed")
        if article and (not article_stream(run.run_request or {}) or any(
            (run.run_request or {}).get(key)!=(operation.payload.get("article_delivery_request") or {}).get(key)
            for key in ("first_article_goal_id","first_article_goal_operation_id"))):
            _scope_error("website_scope_mismatch")
    else:
        # A new remote ID needs the backend's reserved dispatch, not a company
        # name or arbitrary operation asserted by the worker.
        saved = operation.payload or {}
        prefix="article_delivery" if article else "setup_delivery"
        if (saved.get(prefix+"_scope") != scope
                or operation.generation != scope["connection_generation"]
                or saved.get("client_request_id") != scope["client_request_id"]
                or saved.get("article_delivery_workflow" if article else "workflow") != envelope["workflow"]
                or saved.get("run_id") not in (None, "", scope["client_request_id"], envelope["run_id"])
                or operation.state not in {"pending", "running"}):
            _scope_error("website_dispatch_unproven")
    return website, operation, run


def _check_current_scope(website, operation, run, envelope):
    scope = envelope["scope"]
    validate_authority(website, scope, action="read", domain=scope["domain"], github_repo=scope["github_repo"])
    from .website_operations import deletion_epoch
    if (operation.generation != scope["connection_generation"]
            or operation.payload.get("attempt", 1) != scope["operation_attempt"]
            or deletion_epoch(website) != scope["deletion_epoch"]):
        _scope_error("website_operation_changed")
    if operation.state in {"cancelled", "deleted", "denied"} or run and run.status in {"cancelled", "denied"}:
        _scope_error("website_operation_cancelled")
    if operation.state in {"completed", "failed", "blocked"}:
        status = envelope["body"]["state_projection"]["status"]
        if status != operation.state and not (operation.state in {"failed", "blocked"} and status in {"failed", "blocked"}):
            _scope_error("website_operation_terminal")


def _version(envelope):
    execution = envelope["execution"]
    return execution["generation"], execution["state_version"]


def _ordering(run, operation, envelope):
    metadata = _metadata(run)
    latest = metadata.get("latest") or {}
    received = _version(envelope)
    saved = tuple(latest.get("execution", []))
    if not saved:
        result = run.result or {}
        if type(result.get("generation")) is int and type(result.get("state_version")) is int:
            saved = (result["generation"], result["state_version"])
    if saved and received < saved:
        return "ignored_stale"
    if latest and received == saved:
        if latest.get("state_sha256") != envelope["state_sha256"]:
            return "state_conflict"
        return "no_change"
    authorized_resume = False
    if saved and received[0] > saved[0]:
        baseline = operation.payload.get("resume_execution_version")
        if not (operation.payload.get("resume_pending") and isinstance(baseline, (list, tuple))
                and tuple(baseline) == saved):
            return "generation_unproven"
        authorized_resume = True
    if not saved and received[0] != 0:
        return "generation_unproven"
    terminal = run.status in TERMINAL or run.status in {"failed", "blocked"} and not authorized_resume
    if terminal and envelope["body"]["state_projection"]["status"] != run.status:
        return "observation_only"
    return "apply"


def _record_receipt(run, envelope, nonce, disposition, now):
    metadata = _metadata(run)
    receipts = metadata.setdefault("receipts", {})
    existing = receipts.get(envelope["event_id"], {})
    if existing.get("nonce") != nonce:
        return False
    receipts[envelope["event_id"]] = {**existing, "processed_at": now.isoformat(), "disposition": disposition}
    metadata["enrolled"] = True
    _save_metadata(run, metadata)
    ContentFactoryCallbackEvent.objects.filter(event_id=envelope["event_id"],
        job_id=envelope["run_id"]).update(processed_at=now)
    return True


def _proof_guard(envelope):
    """Verify remote source before locks when a snapshot carries scan evidence."""
    if envelope["workflow"] in ARTICLE_WORKFLOWS:
        return nullcontext(None)
    result = envelope["body"]["state_projection"]["result"]
    original = ContentFactoryRun.objects.filter(run_id=envelope["run_id"]).first()
    if (original is not None and original.status in {"completed", "failed", "blocked"}
            and envelope["body"]["state_projection"]["status"] != original.status):
        return nullcontext(None)
    evidence_keys = {"repository_inventory", "tech_stack", "repo_execution_contract", "article_system", "publish_targets"}
    if not any(result.get(key) for key in evidence_keys):
        return nullcontext(None)
    from .website_connections import authority_guard
    data = {**deepcopy(result), **envelope["scope"], "run_id": envelope["run_id"],
        "workflow": envelope["workflow"], "status": envelope["body"]["state_projection"]["status"]}
    return authority_guard(data, action="config_write")


def _setup_proof_projection(run, website, envelope):
    """A claimed setup milestone needs the existing exact-commit verifier.

    Ordinary progress remains visible. A bare passed/completed label cannot
    become preview, approval, merge or publication evidence in saved state.
    """
    projection = deepcopy(envelope["body"]["state_projection"])
    result = projection["result"]
    setup = result.get("article_system_setup") or {}
    gates = result.get("directory_quality_gates") or {}
    proof = result.get("directory_quality_verification") or {}
    promotive_statuses = {"pr_created", "setup_pr_created", "adopted", "preview_ready", "published", "merged", "approved", "ready"}
    proof_flags = {"build_verified", "browser_verified", "adopted", "adoption_kit_preview_verified",
        "generation_ready", "publishing_ready", "generationReady", "publishingReady", "preview_content_verified",
        "route_is_live", "approved", "approval_recorded", "publishing_verified", "exactRender", "exact_render"}
    def promotive(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key in {"status", "state"} and isinstance(item, str)
                        and item in promotive_statuses | {"passed", "verified", "preview_verified", "success", "live"}):
                    return True
                if item is True and (key in proof_flags or key.endswith(("_verified", "_ready", "_approved", "_passed"))):
                    return True
                if promotive(item):
                    return True
        elif isinstance(value, list):
            return any(promotive(item) for item in value)
        return False
    claims = (promotive(projection["verification_summary"]) or promotive(projection["acceptance_summary"])
        or gates.get("status") in {"passed", "verified"} or proof.get("status") in {"passed", "verified"}
        or any(item.get("status") in promotive_statuses or any(item.get(key) is True for key in proof_flags)
               for item in (result, setup, result.get("live_preview") or {}))
        or projection["approval_state"] in {"approved", "denied"} and projection["approval_state"] != run.approval_state)
    if not claims:
        # Sparse/negative progress cannot erase previously accepted facts. New
        # candidate proof replaces this history only after the exact verifier.
        projection["verification_summary"] = deepcopy(run.verification_summary or projection["verification_summary"])
        projection["acceptance_summary"] = deepcopy(run.acceptance_summary or projection["acceptance_summary"])
        protected = {"directory_quality_gates", "directory_quality_verification", "directory_build_verification",
            "source_sha", "branch_commit_sha", "preview_commit_sha", *proof_flags}
        for key in protected:
            if key in (run.result or {}):
                projection["result"][key] = deepcopy(run.result[key])
        old_setup = (run.result or {}).get("article_system_setup")
        if isinstance(old_setup, dict):
            nested = projection["result"].get("article_system_setup") or {}
            for key in protected | {"status"}:
                if key in old_setup:
                    nested[key] = deepcopy(old_setup[key])
            projection["result"]["article_system_setup"] = nested
        return projection, True
    from .website_connections import _verified_setup_run_base
    candidate = copy(run)
    candidate.status, candidate.approval_state = projection["status"], projection["approval_state"]
    if candidate.approval_state in {"approved", "denied"}:
        candidate.approval_state = run.approval_state
    candidate.result = result
    head = result.get("branch_commit_sha") or result.get("preview_commit_sha") or ""
    if (envelope["workflow"] == "article_system_setup"
            and _verified_setup_run_base(candidate, head, website, automatic_approval=True)):
        return projection, True
    from .run_observations import _observations
    safe = _observations(result)
    if isinstance(result.get("article_system_setup"), dict):
        safe["article_system_setup"] = _observations(result["article_system_setup"])
    proof_fields = {"directory_quality_gates", "directory_quality_verification", "directory_build_verification", *proof_flags}
    for item in (safe, safe.get("article_system_setup")):
        if isinstance(item, dict):
            for key in proof_fields:
                item.pop(key, None)
            if item.get("status") in promotive_statuses:
                item.pop("status", None)
    previous = deepcopy(run.result or {})
    old_setup = previous.get("article_system_setup")
    if isinstance(old_setup, dict) and isinstance(safe.get("article_system_setup"), dict):
        safe["article_system_setup"] = {**old_setup, **safe["article_system_setup"]}
        for key in proof_fields | {"status", "source_sha", "branch_commit_sha", "preview_commit_sha"}:
            if key in old_setup:
                safe["article_system_setup"][key] = old_setup[key]
    safe = {**previous, **safe}
    for key in proof_fields | {"status", "source_sha", "branch_commit_sha", "preview_commit_sha"}:
        if key in previous:
            safe[key] = previous[key]
    projection["result"] = safe
    projection["verification_summary"] = deepcopy(run.verification_summary or {})
    projection["acceptance_summary"] = deepcopy(run.acceptance_summary or {})
    projection["approval_state"] = run.approval_state
    return projection, False


def _project_state(run, operation, envelope, *, website, proof_connection=None):
    """Persist validated current state without notification, refund or action."""
    from .service_views import _merge_django_owned_run_result
    from .website_operations import observe_workflow_status
    if envelope["workflow"] in ARTICLE_WORKFLOWS:
        # Durable article transport records the outcome, not publication proof.
        # The canonical article reader still verifies quality/approval/live
        # evidence. Sparse callbacks cannot erase its richer saved evidence.
        projection=deepcopy(envelope["body"]["state_projection"])
        observed={key:value for key,value in projection["result"].items() if key in {
            "failure","failure_occurrence_id","failure_class","failure_fingerprint","error_code","retryable","usage","failed_step","repair_state"}}
        projection["result"]={**deepcopy(run.result or {}),**observed}
        projection["verification_summary"]=deepcopy(run.verification_summary or {})
        projection["acceptance_summary"]=deepcopy(run.acceptance_summary or {})
        authoritative=True
    else:
        projection, authoritative = _setup_proof_projection(run, website, envelope)
    # Approval is owned by the backend. Worker labels never supply that proof.
    incoming_approval = projection["approval_state"]
    if run.approval_state in {"approved", "denied"} or incoming_approval in {"approved", "denied"} and incoming_approval != run.approval_state:
        incoming_approval = run.approval_state
    run.status, run.current_step = projection["status"], projection["current_step"]
    run.approval_state, run.resume_available = incoming_approval, projection["resume_available"]
    run.verification_summary, run.acceptance_summary = projection["verification_summary"], projection["acceptance_summary"]
    incoming = projection["result"]
    incoming.update(generation=projection["generation"], state_version=projection["state_version"],
                    failure=projection["failure"], failure_occurrence_id=projection["failure_occurrence_id"])
    run.result = _merge_django_owned_run_result(run.result, incoming)
    failure = projection["failure"]
    run.error = failure.get("user_message", "") if failure else ""
    run.last_event_emitted_at = parse_datetime(envelope["emitted_at"])
    run.save(update_fields=["status", "current_step", "approval_state", "resume_available",
        "verification_summary", "acceptance_summary", "result", "error", "last_event_emitted_at", "updated_at"])
    for index, (step_key, state) in enumerate(projection["step_states"].items()):
        ContentFactoryRunStep.objects.update_or_create(run=run, step_key=step_key,
            defaults={"display_order": index, "status": state["status"],
                      "required": state["required"], "attempts": state["attempts"]})
    metadata = _metadata(run)
    metadata["latest"] = {"execution": list(_version(envelope)), "state_sha256": envelope["state_sha256"],
        "scope": deepcopy(envelope["scope"]), "received_at": timezone.now().isoformat()}
    metadata["waiting_acknowledged"] = run.status in {"awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode"}
    if projection["failure_occurrence_id"] and failure:
        occurrences = metadata.setdefault("failure_occurrences", {})
        key = projection["failure_occurrence_id"]
        # Retain one actual causal occurrence across callback/mirror/pull. B4
        # owns counting when a goal exists; delivery never invents that goal.
        occurrences.setdefault(key, {"failure": deepcopy(failure), "execution": list(_version(envelope))})
    _save_metadata(run, metadata)
    observe_workflow_status(run, {"status": run.status, **envelope["execution"]})
    if proof_connection is not None:
        from .website_connections import check_source_identity, record_scan_evidence
        result = projection["result"]
        proof_payload = {**deepcopy(result), **envelope["scope"], "run_id": run.run_id}
        check_source_identity(proof_connection, proof_payload, required=True)
        record_scan_evidence(proof_connection, proof_payload)
    return authoritative


def receive_setup_delivery(payload, *, expected_kind=None, path_run_id=None, pull_pin=None):
    """Validate before writes, claim durably, then apply under original fences."""
    from .setup_delivery_contract import DeliveryContractError, validate_envelope
    try:
        envelope = validate_envelope(payload, expected_kind=expected_kind, path_run_id=path_run_id)
    except DeliveryContractError as exc:
        logger.warning("setup_delivery_contract_rejected code=%s fields=%s", exc.code, exc.paths)
        return Response({"code": exc.code, "retryable": False, "acknowledged": False,
                         "fieldErrors": [{"path": path} for path in exc.paths]}, status=422)
    now, nonce = timezone.now(), uuid4().hex
    try:
        with transaction.atomic():
            website, operation, run = _locked_context(envelope)
            if run is not None:
                metadata = _metadata(run)
                receipt = metadata.get("receipts", {}).get(envelope["event_id"])
                if receipt:
                    if receipt.get("payload_sha256") != envelope["payload_sha256"]:
                        return Response(acknowledgement(envelope, "quarantined", code="delivery_identity_conflict",
                            acknowledged=False), status=422)
                    if receipt.get("processed_at"):
                        return Response(acknowledgement(envelope, "duplicate"), status=200)
                    claimed = parse_datetime(receipt.get("claimed_at", ""))
                    if claimed and claimed > now - timedelta(seconds=LEASE_SECONDS):
                        return Response(acknowledgement(envelope, "pending", code="delivery_claim_pending",
                            acknowledged=False, retryable=True), status=409)
                if pull_pin is not None and not pull_pin_matches(run, pull_pin):
                    return Response(acknowledgement(envelope, "pending", code="delivery_pull_superseded",
                        acknowledged=False, retryable=True), status=409)
                before = _ordering(run, operation, envelope)
                if before in {"state_conflict", "generation_unproven"}:
                    return Response(acknowledgement(envelope, "quarantined", code="delivery_" + before,
                        acknowledged=False), status=422)
                projection = envelope["body"]["state_projection"]
                occurrence = _metadata(run).get("failure_occurrences", {}).get(projection["failure_occurrence_id"])
                if occurrence and projection["failure"] and (occurrence.get("failure") != projection["failure"]
                        or occurrence.get("execution", [None])[0] != envelope["execution"]["generation"]):
                    return Response(acknowledgement(envelope, "quarantined", code="delivery_failure_occurrence_conflict",
                        acknowledged=False), status=422)
            historical = run is not None and run.status in {"completed", "failed", "blocked"} and before in {"observation_only", "ignored_stale"}
            if not historical:
                _check_current_scope(website, operation, run, envelope)
            if not apply_enabled(envelope):
                return Response(acknowledgement(envelope, "pending", code="delivery_apply_disabled",
                    acknowledged=False, retryable=True), status=503)
            if run is None:
                article=envelope["workflow"] in ARTICLE_WORKFLOWS
                prefix="article_delivery" if article else "setup_delivery"
                run = ContentFactoryRun.objects.create(run_id=envelope["run_id"], workflow=operation.payload.get("workflow") if article else envelope["workflow"],
                    organization_id=website.organization_id, domain=envelope["scope"]["domain"],
                    github_repo=envelope["scope"]["github_repo"], run_request={
                        **deepcopy(operation.payload.get(prefix+"_request") or {}), **deepcopy(envelope["scope"]),
                        prefix+"_v1": {"schema_version": 1}})
                operation.payload = {**operation.payload, "run_id": run.run_id}
                operation.save(update_fields=["payload", "updated_at"])
            metadata = _metadata(run)
            receipts = metadata.setdefault("receipts", {})
            if len(receipts) >= RECEIPT_LIMIT and envelope["event_id"] not in receipts:
                return Response(acknowledgement(envelope, "pending", code="delivery_receipt_capacity",
                    acknowledged=False, retryable=True), status=409)
            marker_type = envelope["event_type"] or "run_snapshot"
            marker, created = ContentFactoryCallbackEvent.objects.select_for_update().get_or_create(
                event_id=envelope["event_id"], defaults={"job_id": envelope["run_id"],
                    "event_type": marker_type, "emitted_at": parse_datetime(envelope["emitted_at"]),
                    "claimed_at": now})
            if not created and (marker.job_id != envelope["run_id"] or marker.event_type != marker_type
                    or envelope["event_id"] not in receipts):
                raise DeliveryReceiptConflict()
            marker.claimed_at, marker.processed_at = now, None
            marker.save(update_fields=["claimed_at", "processed_at"])
            receipts[envelope["event_id"]] = {"payload_sha256": envelope["payload_sha256"],
                "state_sha256": envelope["state_sha256"], "nonce": nonce, "claimed_at": now.isoformat(),
                "processed_at": None}
            metadata["enrolled"] = True
            _save_metadata(run, metadata)
        # Claim and state commit are separate, so a hard death leaves a real
        # reclaimable obligation. No network request occurs under either lock.
        with _proof_guard(envelope) as proof_connection, transaction.atomic():
            website, operation, run = _locked_context(envelope)
            receipt = _metadata(run).get("receipts", {}).get(envelope["event_id"], {})
            if receipt.get("nonce") != nonce:
                return Response(acknowledgement(envelope, "pending", code="delivery_claim_superseded",
                    acknowledged=False, retryable=True), status=409)
            if pull_pin is not None and not pull_pin_matches(run, pull_pin):
                return Response(acknowledgement(envelope, "pending", code="delivery_pull_superseded",
                    acknowledged=False, retryable=True), status=409)
            decision = _ordering(run, operation, envelope)
            if decision in {"state_conflict", "generation_unproven"}:
                return Response(acknowledgement(envelope, "quarantined", code="delivery_" + decision,
                    acknowledged=False), status=422)
            if decision == "apply":
                _check_current_scope(website, operation, run, envelope)
                authoritative = _project_state(run, operation, envelope, website=website,
                    proof_connection=proof_connection)
                disposition = "applied" if authoritative else "observation_only"
            else:
                disposition = "duplicate" if decision == "no_change" else decision
            _record_receipt(run, envelope, nonce, disposition, now)
            if pull_pin is not None and disposition == "applied":
                from .website_operational_alert_delivery import reserve_repair
                reserve_repair(website, run, envelope, now=now)
            return Response(acknowledgement(envelope, disposition), status=200)
    except DeliveryReceiptConflict:
        return Response(acknowledgement(envelope, "quarantined", code="delivery_identity_conflict",
            acknowledged=False), status=422)
    except (WebsiteAuthorityError, DeliveryScopeError) as exc:
        # Only an exact original run binding can acknowledge historical fenced
        # bytes; unknown/scope-conflicting input is never a successful receipt.
        original = ContentFactoryRun.objects.filter(run_id=envelope["run_id"]).first()
        exact = original is not None and _original_scope_matches(original, envelope["scope"])
        code = getattr(exc, "authority_code", exc.code)
        terminal = exact and code in {"website_operation_terminal", "website_operation_cancelled",
            "website_disconnected", "website_connection_changed", "website_operation_changed"}
        disposition = "ack_terminal" if terminal else "rejected"
        receipt = acknowledgement(envelope, disposition,
            code="website_operation_terminal" if terminal else code,
            acknowledged=terminal, retryable=exc.retryable)
        if terminal:
            receipt["reason_code"] = code
        return Response(receipt, status=exc.status)


def pull_pin_matches(run, pin):
    """A concurrent state, scope or lease change invalidates a pull candidate."""
    metadata = _metadata(run)
    return (metadata.get("pull", {}).get("nonce") == pin.get("nonce")
        and run.run_request == pin.get("run_request") and run.status == pin.get("status")
        and metadata.get("latest") == pin.get("latest")
        and (run.result or {}).get("generation") == pin.get("generation")
        and (run.result or {}).get("state_version") == pin.get("state_version"))
