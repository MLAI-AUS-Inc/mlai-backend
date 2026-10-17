"""Durable bounded verification of an encrypted original owner credential grant."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_access_authority as authority
from . import cms_access_contract as contract
from . import cms_goal_budget as budget
from . import website_goal_budget as shared_budget
from . import cms_credential_renewal as renewal
from . import cms_provider_identity as provider
from .cms_api_transport import CmsApiError, request_json
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import classify_failure

LEASE_SECONDS = 180
MAX_ATTEMPTS = 20


class AccessBudgetStop(ValueError):
    """The original site's bounded allowance cannot admit another access GET."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("The original CMS access budget is exhausted.")


def _write(job, *, state, status, now, receipt=None, delay=30, wipe=False):
    job.payload = {key: item for key, item in (job.payload.items() if isinstance(job.payload, dict) else []) if key != "lease_id"}
    if wipe: job.payload["sealed_credential"] = None
    job.state = state
    job.next_attempt_at = now + timedelta(seconds=delay) if state == "pending" else None
    job.receipt = {**deepcopy(receipt or {}), "status": status}
    job.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
    return job


def _account(value, *, now, active_seconds=0, provider_seconds=0):
    root, original = value.root, value.value
    reason = shared_budget.exhaustion(value.budget, now=now, action="cms-access")
    if reason is not None:
        raise AccessBudgetStop(reason)
    selected = budget.reserve_access_usage(root.receipt.get("native_budget"), started_at=original.started_at,
        operation_id=str(root.pk), access_operation_id=str(value.job.pk), active_seconds=active_seconds,
        provider_seconds=provider_seconds, now=now)
    root.receipt = {**root.receipt, "native_budget": selected}
    root.save(update_fields=["receipt", "updated_at"])


def _stop(value, *, now, reason, failure=None, occurrence=None, effective=None):
    """Stop the original parent and retain its existing settlement authority."""
    root = value.root
    root.state, root.next_attempt_at = "needs_attention", None
    root.receipt = {**root.receipt, "status": "native_recovery_required", "budget_stop_reason": reason,
        "failure": failure, "failure_occurrence_id": occurrence, "effective_failure_class": effective, "user_action": None}
    root.save(update_fields=["receipt", "state", "next_attempt_at", "updated_at"])
    from . import cms_goal_refunds, cms_setup_refunds
    try:
        (cms_goal_refunds if root.action == "cms-goal" else cms_setup_refunds).reserve_refund(root, now=now)
    except WebsiteAuthorityError:
        root.receipt = {**root.receipt, "financial_settlement": "verification_required"}
        root.save(update_fields=["receipt", "updated_at"])
    return _write(value.job, state="needs_attention", status="access_budget_stopped", now=now, wipe=True,
        receipt={"budget_stop_reason": reason, **({"failure": failure, "failure_occurrence_id": occurrence,
            "effective_failure_class": effective} if failure is not None else {})})


def _expire(job, *, now):
    """Expiry and cancellation wipe proposals even while execution is paused."""
    intent = authority.historical(job)
    if contract.goals._time(intent["expires_at"]) > now:
        return False
    _write(job, state="needs_user", status="access_expired", now=now, wipe=True,
        receipt={"failure": classify_failure("missing_or_expired_credentials", "cms_access_expiry")})
    return True


def _guard(dispatch):
    from .website_models import WebsiteConnectionOperation
    if not isinstance(dispatch, authority.CmsAccessDispatch): raise authority._deny()
    job = WebsiteConnectionOperation.objects.filter(pk=dispatch.operation_id, action=authority.ACTION).first()
    if job is None: raise authority._deny()
    return authority.current(job, lease_id=dispatch.lease_id)


def _read(dispatch, binding, credential, **options):
    from organizations.models import Organization
    current = _guard(dispatch)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=current.job.connection.organization_id)
        current = _guard(dispatch)
        _account(current, now=timezone.now(), provider_seconds=LEASE_SECONDS)
    current = _guard(dispatch)
    if binding != current.intent["binding"] or credential is not None and credential != current.credential:
        raise authority._deny()
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    response = request_json(binding, None if credential is None else current.credential, **options)
    _guard(dispatch)
    return response


def _failure(candidate, lease, error, *, now, worker_lost=False):
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=candidate.pk, action=authority.ACTION,
            state="running", payload__lease_id=lease).first()
        if job is None: return None
        try:
            if _expire(job, now=now): return job
        except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return _write(job, state="cancelled", status="authority_revoked", now=now, wipe=True)
        try: value = authority.owner_scope(job, now=now)
        except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return _write(job, state="cancelled", status="authority_revoked", now=now, wipe=True)
        if not authority.enabled():
            return _write(job, state="pending", status="access_paused", now=now, delay=300)
        if isinstance(error, AccessBudgetStop):
            return _stop(value, now=now, reason=error.reason)
        lost = worker_lost or job.next_attempt_at is None or job.next_attempt_at <= now
        transient = lost or isinstance(error, CmsApiError) and error.category in {"transient", "origin_unavailable"} or isinstance(error, WebsiteAuthorityError) and error.retryable
        needs_owner = isinstance(error, CmsApiError) and error.category == "credentials_required"
        failure = classify_failure("RUN_STALLED_WORKER_LOST" if lost else "CONNECTION_SERVICE_TRANSIENT" if transient
            else "missing_or_expired_credentials" if needs_owner else "PUBLISHING_ADAPTER_REQUIRED", "cms_access_controller")
        selected = value.budget
        occurrence = f"cms-access:{job.pk}:{job.attempts}:{failure['failure_fingerprint']}"
        selected, effective, fresh = shared_budget.record_policy_occurrence(selected, occurrence=occurrence,
            fingerprint=failure["failure_fingerprint"], classification=failure["failure_class"])
        if fresh: selected["attempts_by_class"][failure["failure_class"]] += 1
        reason = shared_budget.exhaustion(selected, now=now, action="cms-access")
        again = transient and effective in {"transient", "repairable"} and job.attempts < MAX_ATTEMPTS and reason is None
        selected = shared_budget.update_owner_wait(selected, waiting=needs_owner, now=now)
        value.root.receipt = {**value.root.receipt, "native_budget": selected}
        if not again and not needs_owner:
            return _stop(value, now=now, reason=reason or "access_failure_policy", failure=failure, occurrence=occurrence, effective=effective)
        value.root.save(update_fields=["receipt", "state", "next_attempt_at", "updated_at"])
        job.payload = {**job.payload, "failures": min(job.payload["failures"] + 1, MAX_ATTEMPTS)}
        return _write(job, state="pending" if again else "needs_user" if needs_owner else "needs_attention",
            status="retrying_access" if again else "owner_action_required" if needs_owner else "access_budget_stopped", now=now,
            receipt={"failure": failure, "effective_failure_class": effective, "failure_occurrence_id": occurrence},
            delay=min(300, 15 * 2 ** min(job.payload["failures"], 5)), wipe=not again)


def advance(identifier, *, now=None):
    """Resume the private grant after page/worker loss; never start model work."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None or candidate.state not in authority.ACTIVE: return candidate
    now, lease, claimed, observed_attempt = now or timezone.now(), str(uuid4()), False, candidate.attempts
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
            if job is None or job.state not in authority.ACTIVE: return job
            observed_attempt = job.attempts
            if _expire(job, now=now): return job
            if not authority.enabled() or job.next_attempt_at and job.next_attempt_at > now: return job
            if job.state == "running":
                return _failure(job, job.payload.get("lease_id"), None, now=now, worker_lost=True)
            value = authority.current(job, now=now)
            if job.attempts >= MAX_ATTEMPTS:
                return _stop(value, now=now, reason="access_attempts")
            try:
                _account(value, now=now, active_seconds=LEASE_SECONDS)
            except AccessBudgetStop as error:
                return _stop(value, now=now, reason=error.reason)
            job.state, job.next_attempt_at, job.attempts = "running", now + timedelta(seconds=LEASE_SECONDS), job.attempts + 1
            job.payload = {**job.payload, "lease_id": lease}
            job.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
        claimed = True
        dispatch = authority.CmsAccessDispatch(str(job.pk), lease)
        current = _guard(dispatch)
        identity = provider.inspect_identity(current.intent["binding"], current.credential,
            read=lambda binding, credential, **options: _read(dispatch, binding, credential, **options))
        if (identity.get("can_publish_posts") is not True or identity.get("principal_id") != current.intent["principal_id"]):
            raise CmsApiError("credentials_required")
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=job.connection.organization_id)
            current = _guard(dispatch)
            intent = current.intent
            renewal.renew(current.selected, current.value.context, current.value.config, user=current.value.user, proposal=intent["binding"],
                credential=current.credential, identity=identity, request_id=intent["request_id"],
                operation_key=f"cms-connect:{intent['binding']['organization_id']}:{intent['binding']['company_id']}:{intent['request_id']}")
            return _write(current.job, state="completed", status="access_updated", now=timezone.now(), wipe=True,
                receipt={"intent_digest": intent["digest"], "verified_at": timezone.now().isoformat()})
    except (WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        if claimed:
            return _failure(job, lease, error, now=max(now, timezone.now()))
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION,
                state__in=authority.ACTIVE, attempts=observed_attempt).first()
            return _write(job, state="cancelled", status="authority_revoked", now=now, wipe=True) if job is not None else None


def cancel_for_connection(connection):
    """Wipe proposed keys before the owning lifecycle changes its generation."""
    from .website_models import WebsiteConnectionOperation
    for job in WebsiteConnectionOperation.objects.select_for_update().filter(connection_id=connection.pk, action=authority.ACTION):
        if isinstance(job.payload, dict) and job.payload.get("sealed_credential") is not None:
            _write(job, state="cancelled", status="authority_revoked", now=timezone.now(), wipe=True)
