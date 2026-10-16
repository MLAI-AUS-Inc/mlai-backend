"""Durable, read-only CMS capture with one bounded provider page per lease."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from .cms_api_transport import request_json, CmsApiError
from .cms_connection_authority import current_credential, enabled
from .cms_connection_contract import CmsContractError
from .cms_inventory_contract import new_inventory, page_options, apply_page, validate_inventory
from .cms_operation_scope import current_scope
from .cms_owner_connection import _owner, _request_id
from .website_contract import WebsiteAuthorityError, connection_contract

ACTION = "cms-capture"
LEASE_SECONDS = 60


def _consent(context, config, user, data):
    _owner(context, config, user)
    connection = config.website_connection
    expected = connection_contract(data)
    if (connection is None or not expected or expected.get("repository_id") is not None or expected.get("connection_target_id") or
            expected["website_connection_id"] != str(connection.pk) or expected["connection_generation"] != connection.generation):
        raise WebsiteAuthorityError("website_connection_changed", "Review the current CMS connection before capturing posts.")
    return current_credential(context, config, connection, user=user)[0]


def start_capture(context, config, *, user, data):
    """Reserve one original owner request; repeated keys reuse the same capture."""
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS capture is not available.", status=404)
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    request_id = _request_id(data)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        context.organization.refresh_from_db()
        context.profile.refresh_from_db()
        context.company.refresh_from_db()
        user.refresh_from_db(fields=["is_active"])
        config.refresh_from_db()
        binding = _consent(context, config, user, data)
        reference = config.website_connection.capabilities["_cms_credential_operation_id"]
        operation, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"cms-capture:{binding['connection_id']}:{binding['generation']}:{request_id}",
            defaults={"connection": config.website_connection, "generation": binding["generation"], "action": ACTION,
                "state": "pending", "next_attempt_at": timezone.now(),
                "payload": {"schema_version": 1, "binding": binding, "credential_operation_id": reference,
                    "inventory": new_inventory(binding), "page_failures": 0, "restarts": 0},
                "receipt": {"status": "capture_pending", "channel": "cms", "coverage": "pending", "count": 0,
                    "provider_modified": False, "publishing_verified": False}})
        if (operation.action != ACTION or operation.payload.get("binding") != binding or
                operation.payload.get("credential_operation_id") != reference):
            raise WebsiteAuthorityError("operation_key_conflict", "The saved CMS capture belongs to a different connection.")
        return operation


def _progress(inventory):
    return {"status": "capture_pending" if inventory["coverage"] == "pending" else "capture_complete" if inventory["coverage"] == "complete" else "capture_partial",
        "channel": "cms", "coverage": inventory["coverage"], "count": len(inventory["records"]),
        "excludedCount": inventory["excluded_count"], "pages": inventory["observed_pages"],
        "inventoryDigest": inventory["digest"], "limitReason": inventory["limit_reason"],
        "provider_modified": False, "publishing_verified": False}


def _failure(operation, category, now):
    """Use the shared B1 contract and bounded same-page/collection recovery."""
    from .website_failure_contract import classify_failure
    failures = operation.payload.get("page_failures", 0) + 1
    operation.payload["page_failures"] = failures
    if category == "inventory_changed" and operation.payload.get("restarts", 0) < 2:
        operation.payload["inventory"] = new_inventory(operation.payload["binding"])
        operation.payload["restarts"] = operation.payload.get("restarts", 0) + 1
        operation.payload["page_failures"] = 0
        failure = classify_failure("SOURCE_CAPTURE_UNAVAILABLE", "cms_capture")
        operation.state, operation.next_attempt_at = "pending", now + timedelta(seconds=1)
    elif category in {"transient", "origin_unavailable"} and failures < 5:
        failure = classify_failure("SOURCE_CAPTURE_UNAVAILABLE", "cms_capture")
        operation.state, operation.next_attempt_at = "pending", now + timedelta(seconds=15 * 2 ** (failures - 1))
    elif category == "credentials_required":
        failure = classify_failure("missing_or_expired_credentials", "cms_capture")
        operation.state, operation.next_attempt_at = "needs_user", None
    else:
        failure = classify_failure("SOURCE_CAPTURE_INVALID", "cms_capture")
        operation.state, operation.next_attempt_at = "blocked", None
    operation.receipt = {**operation.receipt, "status": "capture_retrying" if operation.state == "pending" else "capture_blocked",
        "failure": failure, "failure_class": failure["failure_class"], "category": category,
        "page_failures": failures, "restarts": operation.payload.get("restarts", 0), "provider_modified": False}


def advance_capture(identifier, *, now=None):
    """Claim/read/fence one page; retrying a read cannot duplicate a provider post."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    if not enabled():
        return None
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None:
        return None
    candidate_revision = candidate.updated_at
    lease = str(uuid4())
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if operation is None or operation.state not in {"pending", "running"} or operation.next_attempt_at and operation.next_attempt_at > now:
                return operation
            scope = current_scope(operation)
            payload = operation.payload
            if (set(payload) - {"schema_version", "binding", "credential_operation_id", "inventory", "page_failures", "restarts", "lease_id"} or
                    type(payload.get("schema_version")) is not int or payload["schema_version"] != 1 or
                    type(payload.get("page_failures")) is not int or not 0 <= payload["page_failures"] <= 5 or
                    type(payload.get("restarts")) is not int or not 0 <= payload["restarts"] <= 2):
                raise CmsApiError("invalid_request")
            validate_inventory(operation.payload.get("inventory"), binding=scope.binding)
            operation.payload = {**operation.payload, "lease_id": lease}
            operation.state, operation.next_attempt_at = "running", now + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
        # No transaction or transferable cached credential spans provider HTTP.
        operation.refresh_from_db()
        scope = current_scope(operation, lease_id=lease)
        binding, credential = current_credential(scope.context, scope.config, scope.connection, user=scope.user)
        from .website_connections import require_unlocked_remote_call
        require_unlocked_remote_call()
        failure = None
        try:
            response, pagination = request_json(binding, credential, **page_options(binding, operation.payload["inventory"]))
            inventory = apply_page(operation.payload["inventory"], binding, response, pagination)
        except CmsApiError as exc:
            failure = exc.category
            inventory = None
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=scope.connection.organization_id)
            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if saved is None or saved.state != "running" or saved.payload.get("lease_id") != lease:
                return saved
            current_scope(saved, lease_id=lease)
            saved.payload = deepcopy(saved.payload)
            saved.payload.pop("lease_id", None)
            if failure is not None:
                _failure(saved, failure, now)
            else:
                saved.payload.update(inventory=inventory, page_failures=0)
                saved.receipt = _progress(inventory)
                saved.state = "pending" if inventory["coverage"] == "pending" else "completed"
                saved.next_attempt_at = now if saved.state == "pending" else None
            saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return saved
    except WebsiteAuthorityError as exc:
        # Revocation-only CAS is safe after a changed owner/binding: never persist
        # a provider result or overwrite a cancellation/newer worker's outcome.
        from django.db.models import Q
        owned = Q(state="running", payload__lease_id=lease) | Q(state="pending", updated_at=candidate_revision, payload__lease_id__isnull=True)
        receipt, state = {"status": "authority_changed", "provider_modified": False}, "cancelled"
        if exc.authority_code == "missing_or_expired_credentials":
            from .website_failure_contract import classify_failure
            failure = classify_failure("missing_or_expired_credentials", "cms_capture")
            receipt.update(status="capture_blocked", failure=failure, failure_class=failure["failure_class"])
            state = "needs_user"
        WebsiteConnectionOperation.objects.filter(owned, pk=identifier, action=ACTION).update(
            state=state, next_attempt_at=None, receipt=receipt, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    except (CmsApiError, CmsContractError, ValueError, TypeError, KeyError):
        from django.db.models import Q
        from .website_failure_contract import classify_failure
        owned = Q(state="running", payload__lease_id=lease) | Q(state="pending", updated_at=candidate_revision, payload__lease_id__isnull=True)
        failure = classify_failure("SOURCE_CAPTURE_INVALID", "cms_capture")
        WebsiteConnectionOperation.objects.filter(owned, pk=identifier, action=ACTION).update(
            state="blocked", next_attempt_at=None, receipt={"status": "invalid_capture_contract", "failure": failure,
                "failure_class": failure["failure_class"], "provider_modified": False}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
