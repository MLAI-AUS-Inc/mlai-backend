"""Current private read reservation and source-refresh coordination.

Inspection grants no build, model, Git credential or publication permission.
The reconciliation/fence/seal/execute stages must use separate authority.
"""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from rest_framework.exceptions import APIException

from . import website_goal as goals, website_article_refresh_intent as intents
from .article_source_refresh_contract import (INSPECTION_WORKFLOW, MAX_RECEIPT_BYTES,
    RefreshContractError, make_request, validate_request, validate_inspection)
from .website_contract import WebsiteAuthorityError
from .bounded_service_read import read_limited, ServiceUnavailable

REQUEST_KEY = "article_source_refresh_request"
OPERATION_KEY = "article_source_refresh_inspection_operation_id"
READ_ONLY_KEY = "article_source_refresh_inspection_only"
READ_LEASE = timedelta(seconds=60)


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The current article inspection reservation changed.")


def _inspection_operation(identifier, *, lock=False):
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects
    if lock:
        query = query.select_for_update()
    return query.select_related("connection__organization").filter(pk=identifier, action="workflow").first()


def _validate_operation(operation, parent, request):
    payload = goals.mapping(getattr(operation, "payload", None))
    if (operation is None or operation.state not in {"pending", "running", "completed"}
            or operation.connection_id != parent.connection_id or operation.generation != parent.generation
            or str(operation.pk) != request["operation_id"] or type(payload.get("attempt")) is not int
            or payload["attempt"] != request["operation_attempt"]
            or payload.get("workflow") != INSPECTION_WORKFLOW
            or payload.get(READ_ONLY_KEY) != {"schema_version": 1}
            or type(goals.mapping(payload.get(READ_ONLY_KEY)).get("schema_version")) is not int
            or payload.get("first_article_goal_operation_id") != str(parent.pk)
            or payload.get("client_request_id") != request["client_request_id"]
            or payload.get(REQUEST_KEY) != request or payload.get("run_id") != request["intent"]["proposed_run_id"]
            or parent.receipt.get(OPERATION_KEY) != str(operation.pk)
            or parent.receipt.get(intents.KEY) != request["intent"]):
        _deny()


def reserve_inspection(parent, context, config, *, user, now):
    """Journal a distinct, read-only operation before making the worker request."""
    intent = intents.reserve(parent, context, config, user=user, now=now)
    if intent is None:
        return None
    from .website_connections import authority_guard, contract_for
    from .website_operations import reserve_workflow_operation
    from .website_first_article import _save_fields
    binding = contract_for(parent.connection)
    payload = {**binding, "domain": parent.connection.organization.domain, "github_repo": parent.connection.github_repo,
        "client_request_id": "articles-refresh-inspect:" + intent["id"],
        "first_article_goal_id": str(parent.pk), "first_article_goal_operation_id": str(parent.pk),
        "article_source_refresh_intent_id": intent["id"], READ_ONLY_KEY: {"schema_version": 1}}
    with authority_guard(binding, action="read") as connection:
        operation = reserve_workflow_operation(connection, workflow=INSPECTION_WORKFLOW, payload=payload)
        request = make_request(intent=intent, operation_id=str(operation.pk), operation_attempt=operation.payload["attempt"],
            domain=connection.organization.domain, github_repo=connection.github_repo)
        saved = operation.payload.get(REQUEST_KEY)
        if saved is not None and saved != request:
            _deny()
        if READ_ONLY_KEY in operation.payload and operation.payload[READ_ONLY_KEY] != {"schema_version": 1}:
            _deny()
        operation.payload = {**operation.payload, REQUEST_KEY: request, "run_id": intent["proposed_run_id"], READ_ONLY_KEY: {"schema_version": 1}}
        operation.save(update_fields=["payload", "updated_at"])
        _save_fields(parent, {OPERATION_KEY: str(operation.pk)})
        return operation, request


def admission_receipt(query):
    """Recheck the original owner/company/payment for one credential-free read."""
    from .website_connections import authority_guard, contract_for
    from .website_models import WebsiteConnectionOperation
    from .website_prepare import _prepare_context
    from .website_article_recovery import enabled
    try:
        if (not isinstance(query, dict) or set(query) != {"action", "operation_id", "run_id", "request_digest", "domain"}
                or query["action"] != INSPECTION_WORKFLOW or str(UUID(query["operation_id"])) != query["operation_id"]):
            _deny()
        operation = _inspection_operation(query["operation_id"])
        if operation is None:
            _deny()
        request = validate_request(goals.mapping(operation.payload).get(REQUEST_KEY))
        parent = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
            pk=request["intent"]["binding"]["goal"]["goal_id"], action="prepare").first()
        if (parent is None or not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(parent)
                or query["run_id"] != request["intent"]["proposed_run_id"] or query["request_digest"] != request["digest"]
                or query["domain"] != request["domain"]):
            _deny()
        context, config, user = _prepare_context(parent)
        with authority_guard(contract_for(parent.connection), action="read") as connection:
            _validate_operation(operation, parent, request)
            current = intents._current_binding(parent, context, config, user=user, connection=connection)
            if not intents.binding_matches(request["intent"]["binding"], current):
                _deny()
        return {"allowed": True, "permission_mode": "none", "capabilities": {}, "article_source_refresh": deepcopy(request)}
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RefreshContractError, ObjectDoesNotExist, PermissionDenied, PermissionError, APIException):
        _deny()


def _request_inspection(request):
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for article inspection to become available.", retryable=True)
    require_unlocked_remote_call()
    response = None
    try:
        response = read_limited("POST", f"{remote['base_url']}/api/articles/source-refresh/inspect",
            headers=_content_factory_headers(), body=request, maximum=MAX_RECEIPT_BYTES, deadline=45)
        if response.status_code != 200:
            if response.status_code == 429 or response.status_code >= 500:
                raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original article inspection is temporarily unavailable.", retryable=True)
            _deny()
        return validate_inspection(json.loads(response.content), request)
    except WebsiteAuthorityError:
        raise
    except ServiceUnavailable:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original article inspection could not finish.", retryable=True) from None
    except (ValueError, TypeError, KeyError, RefreshContractError, RecursionError):
        _deny()
    finally:
        if response is not None:
            response.close()


def _claim_inspection(parent, operation, request, *, now):
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    with authority_guard(contract_for(parent.connection), action="read"):
        operation = _inspection_operation(str(operation.pk), lock=True)
        _validate_operation(operation, parent, request)
        lease = goals.mapping(operation.receipt).get("inspection_lease")
        if lease is not None:
            try:
                if (not isinstance(lease, dict) or set(lease) != {"nonce", "request_digest", "expires_at"}
                        or str(UUID(lease["nonce"])) != lease["nonce"] or lease["request_digest"] != request["digest"]
                        or goals.stamp(lease["expires_at"]) is None):
                    _deny()
            except (ValueError, TypeError, KeyError):
                _deny()
            if goals.stamp(lease["expires_at"]) > now:
                return None, goals.stamp(lease["expires_at"])
        # A claim that lost its parent lease cannot start another remote read.
        _save_fields(parent, {})
        lease = {"nonce": str(uuid4()), "request_digest": request["digest"], "expires_at": (now + READ_LEASE).isoformat()}
        operation.state = "running"
        operation.receipt = {**goals.mapping(operation.receipt), "inspection_lease": lease}
        operation.save(update_fields=["state", "receipt", "updated_at"])
        return lease, None


def advance(parent, context, config, *, user, now):
    """Continue a stale paid original into the separately admitted inspection stage."""
    if not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False):
        return False
    from .website_first_article import article_run, merged_source, _save_fields, _wait
    run = article_run(parent, config)
    if run is None:
        return False
    current = parent.receipt["goal"].get("current_source_sha")
    if goals.mapping(run.run_request).get("expected_source_sha") == current:
        return False
    owned = merged_source(parent, run, context)
    if owned is not None and owned["source_sha"] == current:
        return False
    reserved = reserve_inspection(parent, context, config, user=user, now=now)
    if reserved is None:
        return True
    operation, request = reserved
    cached = parent.receipt.get("article_source_refresh_inspection")
    if isinstance(cached, dict) and cached.get("request_digest") == request["digest"]:
        try:
            cached = validate_inspection(cached, request)
            checked = goals.stamp(cached["observed_at"])
            if (checked is not None and now - timedelta(minutes=5) <= checked <= now + timedelta(minutes=5)
                    and cached["source_state_version"] >= goals.mapping(run.result).get("state_version", 0)):
                _wait(parent, now, "Reconciling the original article publication before rebuilding on the changed source.")
                return True
        except RefreshContractError:
            _deny()
    lease, pending_until = _claim_inspection(parent, operation, request, now=now)
    if lease is None:
        _wait(parent, now, "Waiting for the original article inspection to finish.")
        parent.next_attempt_at = max(parent.next_attempt_at, pending_until)
        return True
    receipt = _request_inspection(request)
    from .website_connections import authority_guard, contract_for
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        operation = _inspection_operation(str(operation.pk), lock=True)
        _validate_operation(operation, parent, request)
        binding = intents._current_binding(parent, context, config, user=user, connection=connection)
        if not intents.binding_matches(request["intent"]["binding"], binding):
            _deny()
        if goals.mapping(operation.receipt).get("inspection_lease") != lease or receipt["source_state_version"] < binding["paid_root"]["state_version"]:
            _deny()
        checked = goals.stamp(receipt["observed_at"])
        if checked is None or not now - timedelta(minutes=5) <= checked <= now + timedelta(minutes=5):
            _deny()
        _save_fields(parent, {"article_source_refresh_inspection": receipt, "article_source_refresh_stage": "inspected"})
        operation.state = "completed"
        operation.receipt = {**{key: value for key, value in goals.mapping(operation.receipt).items() if key != "inspection_lease"}, "status": "inspected", "inspection_digest": receipt["digest"],
            "repository_modified": False}
        operation.save(update_fields=["state", "receipt", "updated_at"])
    _wait(parent, now, "Reconciling the original article publication before rebuilding on the changed source.")
    return True
