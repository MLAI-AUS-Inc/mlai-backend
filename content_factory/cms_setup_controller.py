"""Durable pre-policy setup claims, bounded discovery and same-identity promotion."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_setup_authority as authority
from . import cms_setup_billing as billing
from . import cms_setup_start as starts
from . import cms_setup_refunds as refunds
from . import cms_goal_budget as native_budget
from . import website_goal_budget as budget
from . import cms_publication_policy as policies
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import classify_failure, validate_failure
from .channel_artifact_contract import digest

LEASE_SECONDS = 180


class SetupBudgetStop(ValueError):
    """A private policy stop with a fixed budget reason, never provider content."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("The original native setup budget is exhausted.")


def _budget(operation, intent, *, now):
    return native_budget.validate_at(operation.receipt.get("native_budget"), started_at=intent["accepted_at"],
        operation_id=str(operation.pk), now=now)


def _write(operation, *, now, state, status, selected=None, updates=None, delay=30, settle=True):
    operation.payload = {key: value for key, value in operation.payload.items() if key != "lease_id"}
    operation.state = state
    operation.next_attempt_at = now + timedelta(seconds=delay) if state in {"pending", "needs_user"} else None
    operation.receipt = {**operation.receipt, **deepcopy(updates or {}), "status": status}
    if selected is not None:
        operation.receipt["native_budget"] = selected
    if state != "needs_user":
        operation.receipt.pop("user_action", None)
    operation.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
    if settle and state in billing.records.REFUND_TERMINAL:
        try:
            refunds.reserve_refund(operation, now=now)
        except WebsiteAuthorityError:
            operation.receipt = {**operation.receipt, "financial_settlement": "verification_required"}
            operation.save(update_fields=["receipt", "updated_at"])
    return operation


def reserve_provider_read(dispatch):
    """Reserve one real defaults GET allowance just before HTTP, under the current lease."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    if not isinstance(dispatch, authority.CmsSetupDispatch):
        raise authority._deny()
    candidate = WebsiteConnectionOperation.objects.filter(pk=dispatch.operation_id, action=authority.ACTION).first()
    if candidate is None:
        raise authority._deny()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=dispatch.operation_id, action=authority.ACTION).first()
        if operation is None:
            raise authority._deny()
        value, now = authority.current(operation, lease_id=dispatch.lease_id), timezone.now()
        selected = _budget(operation, value.intent, now=now)
        reason = budget.exhaustion(selected, now=now, action="setup")
        if reason is not None:
            raise SetupBudgetStop(reason)
        selected = native_budget.reserve_setup_usage(selected, started_at=value.intent["accepted_at"], operation_id=str(operation.pk),
            provider_seconds=LEASE_SECONDS, now=now)
        operation.receipt = {**operation.receipt, "native_budget": selected}
        operation.save(update_fields=["receipt", "updated_at"])


def _finish(candidate, *, lease, attempt, claimed, now, error=None, reason=None):
    """Only the winning claim can record its real failure or terminal budget stop."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"attempts": attempt, "state__in": list(authority.ACTIVE)}
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=candidate.pk, action=authority.ACTION, **owned).first()
        if operation is None:
            return None
        try:
            intent = authority.historical(operation)
        except WebsiteAuthorityError:
            return _write(operation, now=now, state="needs_attention", status="invalid_setup_intent", settle=False)
        try:
            selected = _budget(operation, intent, now=now)
        except WebsiteAuthorityError:
            return _write(operation, now=now, state="needs_attention", status="invalid_setup_budget")
        if not authority.enabled(operation.connection.organization_id):
            return _write(operation, now=now, state="pending", status="setup_paused", selected=selected, delay=300)
        if reason is not None:
            return _write(operation, now=now, state="stopped", status="budget_stopped", selected=selected,
                updates={"budget_stop_reason": reason})
        try:
            authority.current(operation)
        except WebsiteAuthorityError:
            return _write(operation, now=now, state="cancelled", status="authority_changed", selected=selected)
        if isinstance(error, WebsiteAuthorityError) and error.failure:
            failure = validate_failure(error.failure)
        elif isinstance(error, WebsiteAuthorityError) and error.authority_code == "missing_or_expired_credentials":
            failure = classify_failure("missing_or_expired_credentials", "cms_setup_controller")
        else:
            failure = classify_failure("CONNECTION_SERVICE_TRANSIENT" if isinstance(error, WebsiteAuthorityError) and error.retryable
                else "PUBLISHING_ADAPTER_REQUIRED", "cms_setup_controller")
        occurrence = f"cms-setup:{operation.pk}:{operation.attempts}:{failure['failure_fingerprint']}"
        selected, effective, fresh = budget.record_policy_occurrence(selected, occurrence=occurrence,
            fingerprint=failure["failure_fingerprint"], classification=failure["failure_class"])
        if fresh:
            selected["attempts_by_class"][failure["failure_class"]] += 1
        reason = budget.exhaustion(selected, now=now, action="setup")
        if reason is not None:
            return _write(operation, now=now, state="stopped", status="budget_stopped", selected=selected,
                updates={"budget_stop_reason": reason, "failure": failure, "failure_occurrence_id": occurrence})
        terminal = effective in {"strategy", "unsupported"}
        state = "needs_attention" if terminal else "needs_user" if effective == "needs_user" else "pending"
        selected = budget.update_owner_wait(selected, waiting=state == "needs_user", now=now)
        return _write(operation, now=now, state=state, status="native_recovery_required" if terminal else "owner_action_required" if state == "needs_user" else "retrying",
            selected=selected, updates={"failure": failure, "failure_occurrence_id": occurrence, "effective_failure_class": effective,
                **({"user_action": {"id": "connect_cms"}} if state == "needs_user" else {})}, delay=300 if state == "needs_user" else 30)


def advance_setup(identifier, *, now=None):
    """Resume the accepted site without a browser, new debit or replacement identity."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None or not authority.enabled(candidate.connection.organization_id):
        return None
    if candidate.receipt.get("status") in {"initial_connecting", "initial_access_required"}:
        from .cms_initial_controller import for_root
        return for_root(candidate, now=now)
    now, lease, claimed, attempt = now or timezone.now(), str(uuid4()), False, candidate.attempts
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
            if operation is None or operation.state not in authority.ACTIVE or operation.next_attempt_at and operation.next_attempt_at > now:
                return operation
            attempt = operation.attempts
            value = authority.current(operation)
            from .cms_access_status import defer_parent
            if defer_parent(operation, now=now): return operation
            billing.require_held(operation)
            selected = _budget(operation, value.intent, now=now)
            selected = budget.update_owner_wait(selected, waiting=False, now=now)
            if operation.state == "running":
                failure = classify_failure("RUN_STALLED_WORKER_LOST", "cms_setup_controller")
                occurrence = "setup-worker-loss:" + digest({"operation": str(operation.pk), "lease": operation.payload.get("lease_id")})
                selected, effective, fresh = budget.record_policy_occurrence(selected, occurrence=occurrence,
                    fingerprint=failure["failure_fingerprint"], classification=failure["failure_class"])
                if fresh:
                    selected["attempts_by_class"][failure["failure_class"]] += 1
                operation.receipt = {**operation.receipt, "failure": failure, "failure_occurrence_id": occurrence, "effective_failure_class": effective}
                if effective in {"strategy", "unsupported"}:
                    return _write(operation, now=now, state="needs_attention", status="native_recovery_required", selected=selected)
            reason = budget.exhaustion(selected, now=now, action="setup")
            if reason is not None:
                return _write(operation, now=now, state="stopped", status="budget_stopped", selected=selected, updates={"budget_stop_reason": reason})
            proposed = native_budget.reserve_setup_usage(selected, started_at=value.intent["accepted_at"], operation_id=str(operation.pk),
                active_seconds=LEASE_SECONDS, now=now)
            reason = budget.exhaustion(proposed, now=now, action="setup")
            if reason is not None:
                return _write(operation, now=now, state="stopped", status="budget_stopped", selected=selected, updates={"budget_stop_reason": reason})
            selected = proposed
            operation.payload = {**operation.payload, "lease_id": lease}
            operation.receipt = {**operation.receipt, "native_budget": selected, "status": "discovering_native"}
            operation.receipt.pop("user_action", None)
            operation.state, operation.next_attempt_at, operation.attempts = "running", now + timedelta(seconds=LEASE_SECONDS), attempt + 1
            operation.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "attempts", "updated_at"])
        claimed = True
        dispatch = authority.CmsSetupDispatch(str(operation.pk), lease)
        value = authority.current(operation, lease_id=lease)
        # Existing exact owner policy needs no additional configuration writes.
        if value.policy is None or value.policy["policy"]["review_policy"] != value.intent["answers"]["review_policy"]:
            policies.configure(value.context, value.config, user=value.user, data=authority.policy_data(value), setup_scope=dispatch)
        from .cms_policy_access import recheck
        recheck(dispatch)
        return starts.promote(dispatch, now=timezone.now())
    except SetupBudgetStop as exc:
        return _finish(candidate, lease=lease, attempt=attempt, claimed=claimed, now=max(now, timezone.now()), reason=exc.reason)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return _finish(candidate, lease=lease, attempt=attempt, claimed=claimed, now=max(now, timezone.now()), error=exc)
