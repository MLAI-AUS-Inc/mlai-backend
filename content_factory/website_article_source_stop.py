"""Durable private source-stop outbox, advanced before any new goal effects.

This purpose only removes old effects and records bounded worker observations.
Editorial history and known-publication reads use separate private purposes.
Full inventory and external-resource/task/model reconciliation remain separate
prerequisites. A local stop acknowledgement never means worker quiet.
"""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from rest_framework.exceptions import APIException

from . import website_goal as goals, website_article_source_fence as fences
from .article_source_stop_contract import STOP_ACTION, MAX_RECEIPT_BYTES, StopContractError, make_request, validate_request, validate_receipt
from .bounded_service_read import read_limited, ServiceUnavailable
from .website_contract import WebsiteAuthorityError

ACTION = "article-source-stop"
KEY = "article_source_stop_operation_id"
LEASE = timedelta(seconds=60)
CREDENTIAL_LEASE = timedelta(seconds=120)


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article stop reservation changed.")


def _operation(identifier, *, lock=False):
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects
    if lock:
        query = query.select_for_update()
    return query.select_related("connection__organization").filter(pk=identifier, action=ACTION).first()


def _fresh(parent, config):
    """Recheck original owner, company, spend/refund and exact old run/attempt."""
    from .website_connections import authority_guard, contract_for
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        payload = fences.validated(connection, parent, parent.receipt.get(fences.KEY))
        run = fences._run(connection, payload["binding"]["root_run_id"])
        if run is None:
            _deny()
        fences.read_original(parent, config, run)
        return payload


def _validate(operation, parent, request):
    if (operation is None or operation.action != ACTION or operation.state not in {"pending", "running"}
            or operation.connection_id != parent.connection_id or operation.generation != parent.generation
            or str(operation.pk) != request["operation_id"] or operation.payload != {"request": request, "goal_operation_id": str(parent.pk)}
            or parent.receipt.get(KEY) != str(operation.pk) or request["operation_attempt"] != 1
            or request["source_fence_id"] != parent.receipt.get(fences.KEY)
            or request["source_fence"]["binding"]["goal"]["goal_id"] != str(parent.pk)):
        _deny()


def reserve(parent, config):
    """Persist one private stop identity before HTTP; it cannot reserve paid work."""
    from .website_connections import authority_guard, contract_for
    from .website_models import WebsiteConnectionOperation
    from .website_first_article import _save_fields
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        fence = _fresh(parent, config)
        key = f"{connection.pk}:article-source-stop:{parent.receipt[fences.KEY]}"
        row, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key,
            defaults={"connection": connection, "generation": connection.generation, "action": ACTION,
                "state": "pending", "payload": {}, "receipt": {}})
        if created:
            request = make_request(operation_id=str(row.pk), source_fence_id=parent.receipt[fences.KEY], source_fence=fence,
                domain=connection.organization.domain.casefold(), github_repo=connection.github_repo)
            row.payload = {"request": request, "goal_operation_id": str(parent.pk)}
            row.save(update_fields=["payload", "updated_at"])
        else:
            request = validate_request(goals.mapping(row.payload).get("request"))
            if request["source_fence"] != fence:
                _deny()
        _save_fields(parent, {KEY: str(row.pk)})
        _validate(row, parent, request)
        return row, request


def admission_receipt(query):
    """Only a fresh exact service purpose gets a credential-free stop receipt."""
    from .website_models import WebsiteConnectionOperation
    from .website_prepare import _prepare_context
    from .website_article_recovery import enabled
    try:
        if (not isinstance(query, dict) or set(query) != {"action", "operation_id", "run_id", "request_digest", "domain"}
                or query["action"] != STOP_ACTION or str(UUID(query["operation_id"])) != query["operation_id"]):
            _deny()
        row = _operation(query["operation_id"])
        if row is None:
            _deny()
        request = validate_request(goals.mapping(row.payload).get("request"))
        parent = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
            pk=request["source_fence"]["binding"]["goal"]["goal_id"], action="prepare").first()
        if (parent is None or not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(parent)
                or query["run_id"] != request["source_fence"]["binding"]["root_run_id"]
                or query["request_digest"] != request["digest"] or query["domain"] != request["domain"]):
            _deny()
        _, config, _ = _prepare_context(parent)
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
        _validate(row, parent, request)
        return {"allowed": True, "permission_mode": "none", "capabilities": {}, "article_source_stop": deepcopy(request)}
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, StopContractError, ObjectDoesNotExist, PermissionDenied, PermissionError, APIException):
        _deny()


def _request_stop(request):
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for original article stopping.", retryable=True)
    require_unlocked_remote_call()
    response = None
    try:
        response = read_limited("POST", f"{remote['base_url']}/api/articles/source-refresh/stop", headers=_content_factory_headers(),
            body=request, maximum=MAX_RECEIPT_BYTES, deadline=45)
        if response.status_code != 200:
            if response.status_code == 429 or response.status_code >= 500:
                raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original article stop is temporarily unavailable.", retryable=True)
            _deny()
        return validate_receipt(json.loads(response.content), request)
    except WebsiteAuthorityError:
        raise
    except ServiceUnavailable:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for the original article stop receipt.", retryable=True) from None
    except (ValueError, TypeError, KeyError, StopContractError, RecursionError):
        _deny()
    finally:
        if response is not None:
            response.close()


def _retire_original_credentials(parent, config, request, *, now):
    """Remove captured and precisely source-scoped broker credentials.

    The source fence owns these exact original operation IDs. A goal/source-only
    query would also match a later source-hash reversion and is not authority to
    revoke that separate execution. Only original v2 enrollment can extend the
    captured inventory to a deeper operation. Full coverage/quiet is separate.
    """
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    from .website_tokens import revoke_generation_tokens
    from . import website_article_credentials as credentials
    with authority_guard(contract_for(parent.connection), action="read"):
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
        operation = _operation(request["operation_id"], lock=True)
        _validate(operation, parent, request)
        try:
            local_stop = validate_receipt(operation.receipt.get("local_stop"), request)
        except StopContractError:
            _deny()
        if local_stop != parent.receipt.get("article_source_stop"):
            _deny()
        previous = operation.receipt.get("broker_credential_retirement")
        cursor = None
        if previous is not None:
            if (not isinstance(previous, dict) or type(previous.get("schema_version")) is not int
                    or previous["schema_version"] not in {1, 2} or previous.get("request_digest") != request["digest"]
                    or previous.get("source_fence_id") != request["source_fence_id"]
                    or previous.get("channel") != "website_token_broker" or previous.get("quiet") is not False
                    or previous.get("inventory_complete") is not False or previous.get("effect_authority") is not False):
                _deny()
            if previous["schema_version"] == 2:
                cursor = previous.get("next_inventory_id")
                if previous.get("scope") != "captured_and_source_scoped_operations" or "next_inventory_id" not in previous:
                    _deny()
            elif previous.get("scope") != "captured_original_operations":
                _deny()
        prior = operation.receipt.get("credential_lease")
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
        deeper, next_cursor = credentials.source_page(parent.connection_id, request["source_fence"], after=cursor)
        _save_fields(parent, {})  # exact current prepare claim before provider work
        lease = {"nonce": str(uuid4()), "request_digest": request["digest"],
            "expires_at": (now + CREDENTIAL_LEASE).isoformat()}
        operation.receipt = {**operation.receipt, "credential_lease": deepcopy(lease)}
        operation.save(update_fields=["receipt", "updated_at"])
    observed = []
    identifiers = dict.fromkeys([*(node["operation_id"] for node in request["source_fence"]["binding"]["runs"]), *deeper])
    for identifier in identifiers:
        # Both checks use the original owner/payment/cause. No site lock spans
        # provider HTTP. At most two captured + two scoped operations, one call each.
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
        result = revoke_generation_tokens(parent.connection_id, f"operation:{identifier}", maximum_calls=1)
        if (not isinstance(result, dict) or set(result) != {"revoked", "pending"}
                or any(type(result[key]) is not int or result[key] < 0 for key in result)):
            _deny()
        observed.append({"operation_id": identifier, **result})
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
    with authority_guard(contract_for(parent.connection), action="read"):
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
        operation = _operation(request["operation_id"], lock=True)
        _validate(operation, parent, request)
        if operation.receipt.get("credential_lease") != lease:
            _deny()
        receipt = {"schema_version": 2, "request_digest": request["digest"],
            "source_fence_id": request["source_fence_id"], "channel": "website_token_broker",
            "scope": "captured_and_source_scoped_operations", "operations": observed, "next_inventory_id": next_cursor,
            "inventory_complete": False, "quiet": False, "effect_authority": False}
        operation.receipt = {**{key: value for key, value in operation.receipt.items() if key != "credential_lease"},
            "broker_credential_retirement": receipt}
        _save_fields(parent, {})
        operation.save(update_fields=["receipt", "updated_at"])
        return receipt


def advance(parent, config, *, now):
    """Drain the durable stop before source verification, models, or inspection.

    Return True while full reconciliation is pending. This first contract cannot
    prove quiet; callers must not continue a replacement from its acknowledgement.
    """
    from .website_article_recovery import enabled
    if not parent.receipt.get(fences.KEY) or not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False) or not enabled(parent):
        return False
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields, _wait
    operation, request = reserve(parent, config)
    if parent.receipt.get("article_source_stop") is not None:
        _retire_original_credentials(parent, config, request, now=now)
        from . import website_goal_budget as budget
        reason = budget.exhaustion(parent.receipt["goal"], now=now) if budget.enabled() else None
        if reason:
            from .website_goal_runner import stop_goal
            stop_goal(parent, parent.receipt["goal"], reason=reason, now=now)
            return True
        from .website_article_source_history import advance as seal_original_history
        sealed = seal_original_history(parent, config, now=now)
        if sealed is not None:
            from .website_article_stopped_publications import advance as observe_stopped_publications
            observe_stopped_publications(parent, config, now=now)
        _wait(parent, now, "Reconciling outstanding article work after preserving the original editorial content.")
        return True

    with authority_guard(contract_for(parent.connection), action="read"):
        _fresh(parent, config)
        operation = _operation(str(operation.pk), lock=True)
        _validate(operation, parent, request)
        prior = goals.mapping(operation.receipt).get("stop_lease")
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
                _wait(parent, now, "Waiting for the saved original article stop to finish.")
                return True
        _save_fields(parent, {})  # exact prepare claim CAS before the HTTP lease
        lease = {"nonce": str(uuid4()), "request_digest": request["digest"], "expires_at": (now + LEASE).isoformat()}
        operation.state = "running"
        operation.receipt = {**goals.mapping(operation.receipt), "stop_lease": lease}
        operation.save(update_fields=["state", "receipt", "updated_at"])
    receipt = _request_stop(request)
    with authority_guard(contract_for(parent.connection), action="read"):
        if _fresh(parent, config) != request["source_fence"]:
            _deny()
        operation = _operation(str(operation.pk), lock=True)
        _validate(operation, parent, request)
        if operation.receipt.get("stop_lease") != lease:
            _deny()
        operation.receipt = {**{key: value for key, value in operation.receipt.items() if key != "stop_lease"},
            "local_stop": receipt, "remote_confirmed": False, "effect_authority": False}
        operation.save(update_fields=["receipt", "updated_at"])
        _save_fields(parent, {"article_source_stop": receipt})
    _retire_original_credentials(parent, config, request, now=now)
    # Removing old effects must get its first pass even after a compute budget
    # was spent. The reconciliation wait still inherits the original wall and
    # class limits; it cannot become an unbounded pre-journey polling loop.
    from . import website_goal_budget as budget
    reason = budget.exhaustion(parent.receipt["goal"], now=now) if budget.enabled() else None
    if reason:
        from .website_goal_runner import stop_goal
        stop_goal(parent, parent.receipt["goal"], reason=reason, now=now)
        return True
    _wait(parent, now, "Reconciling the stopped article's outstanding work before recovery.")
    return True
