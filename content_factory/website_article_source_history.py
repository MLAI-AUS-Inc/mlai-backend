"""Distinct current-purpose outbox for an editorial seal, never replacement authority."""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from rest_framework.exceptions import APIException

from . import website_goal as goals, website_article_source_stop as stops
from .article_source_history_contract import ACTION as PURPOSE, MAX_RECEIPT_BYTES, make_request, validate_request, validate_receipt, stopped_runs, HistoryContractError
from .bounded_service_read import read_limited, ServiceUnavailable
from .website_contract import WebsiteAuthorityError

ACTION = "article-source-history"
KEY = "article_source_history_operation_id"
LEASE = timedelta(seconds=60)


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article history reservation changed.")


def _operation(identifier, *, lock=False):
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects
    if lock:
        query = query.select_for_update()
    return query.select_related("connection__organization").filter(pk=identifier, action=ACTION).first()


def _fresh(parent, config):
    """A stop observation alone never authorizes a historical read."""
    from .website_connections import authority_guard, contract_for
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        fence = stops._fresh(parent, config)
        goal = parent.receipt["goal"]
        scope = goals.scope_for(connection, goal["scope"]["company_id"])
        if (not goals.scope_matches(scope, goal["scope"]) or goal.get("current_source_sha") != connection.verified_sha):
            _deny()
        stop = stops._operation(parent.receipt.get(stops.KEY))
        if stop is None:
            _deny()
        request = stops.validate_request(goals.mapping(stop.payload).get("request"))
        stops._validate(stop, parent, request)
        receipt = stops.validate_receipt(parent.receipt.get("article_source_stop"), request)
        if stop.receipt.get("local_stop") != receipt or request["source_fence"] != fence:
            _deny()
        return request, receipt, scope, connection.verified_sha


def _validate(row, parent, request):
    if (row is None or row.action != ACTION or row.state not in {"pending", "running", "completed"}
            or row.connection_id != parent.connection_id or row.generation != parent.generation
            or str(row.pk) != request["operation_id"] or parent.receipt.get(KEY) != str(row.pk)
            or row.payload != {"request": request, "goal_operation_id": str(parent.pk)}):
        _deny()


def reserve(parent, config):
    """Reserve once per stop cause/current source without a fee or execution identity."""
    from .website_connections import authority_guard, contract_for
    from .website_models import WebsiteConnectionOperation
    from .website_first_article import _save_fields
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        stop, receipt, scope, sha = _fresh(parent, config)
        key = f"{connection.pk}:article-history:{stop['source_fence_id']}:{goals.digest([scope, sha])}"
        row, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key,
            defaults={"connection": connection, "generation": connection.generation, "action": ACTION,
                "state": "pending", "payload": {}, "receipt": {}})
        if created:
            request = make_request(operation_id=str(row.pk), stop_request=stop, stop_receipt=receipt,
                read_scope=scope, current_source_sha=sha)
            row.payload = {"request": request, "goal_operation_id": str(parent.pk)}
            row.save(update_fields=["payload", "updated_at"])
        else:
            request = validate_request(goals.mapping(row.payload).get("request"))
            if (request["stop_request"] != stop or request["stopped_runs"] != stopped_runs(stop, receipt)
                    or request["read_scope"] != scope or request["current_source_sha"] != sha):
                _deny()
        _save_fields(parent, {KEY: str(row.pk)})
        _validate(row, parent, request)
        return row, request


def admission_receipt(query):
    """Check current owner/company/payment/cause/source before a content-only seal."""
    from .website_models import WebsiteConnectionOperation
    from .website_connections import authority_guard, contract_for
    from .website_prepare import _prepare_context
    from .website_article_recovery import enabled
    try:
        if (not isinstance(query, dict) or set(query) != {"action", "operation_id", "run_id", "request_digest", "domain"}
                or query["action"] != PURPOSE or str(UUID(query["operation_id"])) != query["operation_id"]):
            _deny()
        row = _operation(query["operation_id"])
        if row is None:
            _deny()
        request = validate_request(goals.mapping(row.payload).get("request"))
        stop = request["stop_request"]
        parent = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
            pk=stop["source_fence"]["binding"]["goal"]["goal_id"], action="prepare").first()
        if (parent is None or not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(parent)
                or query["run_id"] != stop["source_fence"]["binding"]["root_run_id"]
                or query["request_digest"] != request["digest"] or query["domain"] != stop["domain"]):
            _deny()
        _, config, _ = _prepare_context(parent)
        with authority_guard(contract_for(parent.connection), action="read"):
            current, receipt, scope, sha = _fresh(parent, config)
            _validate(row, parent, request)
            if (request["stop_request"] != current or request["stopped_runs"] != stopped_runs(current, receipt)
                    or request["read_scope"] != scope or request["current_source_sha"] != sha):
                _deny()
        return {"allowed": True, "permission_mode": "none", "capabilities": {}, "article_source_history": deepcopy(request)}
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ObjectDoesNotExist, PermissionDenied, PermissionError, APIException):
        _deny()


def _request_seal(request):
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for original article history.", retryable=True)
    require_unlocked_remote_call()
    response = None
    try:
        response = read_limited("POST", f"{remote['base_url']}/api/articles/source-refresh/history-seal", headers=_content_factory_headers(),
            body=request, maximum=MAX_RECEIPT_BYTES, deadline=45)
        if response.status_code != 200:
            if response.status_code == 429 or response.status_code >= 500:
                raise ServiceUnavailable("Article history unavailable")
            _deny()
        return validate_receipt(json.loads(response.content), request)
    except WebsiteAuthorityError:
        raise
    except ServiceUnavailable:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for original article history.", retryable=True) from None
    except (ValueError, TypeError, KeyError, RecursionError):
        _deny()
    finally:
        if response is not None:
            response.close()


def advance(parent, config, *, now):
    """Commit a content-only observation under an exact claim and independent lease."""
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    row, request = reserve(parent, config)
    with authority_guard(contract_for(parent.connection), action="read"):
        current, observed, scope, sha = _fresh(parent, config)
        if (current != request["stop_request"] or stopped_runs(current, observed) != request["stopped_runs"]
                or scope != request["read_scope"] or sha != request["current_source_sha"]):
            _deny()
        row = _operation(str(row.pk), lock=True)
        _validate(row, parent, request)
        if row.state == "completed":
            receipt = validate_receipt(goals.mapping(row.receipt).get("history_seal"), request)
            _save_fields(parent, {"article_source_history": receipt})
            return receipt
        prior = goals.mapping(row.receipt).get("history_lease")
        if prior is not None:
            try:
                valid = (isinstance(prior, dict) and set(prior) == {"nonce", "request_digest", "expires_at"}
                    and str(UUID(prior["nonce"])) == prior["nonce"] and prior["request_digest"] == request["digest"]
                    and goals.stamp(prior["expires_at"]) is not None)
            except (ValueError, TypeError, AttributeError, KeyError):
                valid = False
            if not valid:
                _deny()
            if goals.stamp(prior["expires_at"]) > now:
                return None
        _save_fields(parent, {})
        lease = {"nonce": str(uuid4()), "request_digest": request["digest"], "expires_at": (now + LEASE).isoformat()}
        row.state, row.receipt = "running", {**goals.mapping(row.receipt), "history_lease": lease}
        row.save(update_fields=["state", "receipt", "updated_at"])
    receipt = _request_seal(request)
    with authority_guard(contract_for(parent.connection), action="read"):
        current, observed, scope, sha = _fresh(parent, config)
        if (current != request["stop_request"] or stopped_runs(current, observed) != request["stopped_runs"]
                or scope != request["read_scope"] or sha != request["current_source_sha"]):
            _deny()
        row = _operation(str(row.pk), lock=True)
        _validate(row, parent, request)
        if row.receipt.get("history_lease") != lease:
            _deny()
        row.state, row.receipt = "completed", {"history_seal": receipt, "effect_authority": False}
        row.save(update_fields=["state", "receipt", "updated_at"])
        _save_fields(parent, {"article_source_history": receipt})
    return receipt
