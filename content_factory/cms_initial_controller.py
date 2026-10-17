"""Durable initial identity GETs, preserving the original setup and payment."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_initial_authority as authority
from . import cms_goal_budget as native_budget
from . import website_goal_budget as budget
from . import cms_provider_identity as provider
from . import cms_setup_refunds as refunds
from .cms_connection_contract import seal_credential
from .cms_access_contract import wait_epoch
from .cms_api_transport import CmsApiError, request_json
from .channel_artifact_contract import canonical_bytes
from .website_publishing_channels import scope_for
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import classify_failure

LEASE_SECONDS = 180
MAX_ATTEMPTS = 20


class InitialBudgetStop(ValueError):
    """A fixed original budget reason; never provider/key content."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("The original initial CMS budget is exhausted.")


def _job(job, *, state, status, now, receipt=None, delay=30):
    job.payload = {key: item for key, item in (job.payload.items() if isinstance(job.payload, dict) else []) if key not in {"lease_id", "lease_grant_id"}}
    job.state, job.next_attempt_at = state, now + timedelta(seconds=delay) if state == "pending" else None
    job.receipt = {**job.receipt, **deepcopy(receipt or {}), "status": status}
    job.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
    return job


def _wipe(job, *, state, status):
    from .website_models import WebsiteConnectionOperation
    from django.db.models import Q
    reference = job.payload.get("grant_operation_id") if isinstance(job.payload, dict) else None
    try: authority.grants.uuid(reference)
    except (ValueError, TypeError, AttributeError): reference = None
    selected = Q(payload__grant__stage_operation_id=str(job.pk))
    if reference is not None: selected |= Q(pk=reference)
    for row in WebsiteConnectionOperation.objects.select_for_update().filter(action=authority.GRANT_ACTION,
            connection_id=job.connection_id, generation=job.generation).filter(selected):
        row.payload = {**row.payload, "sealed_credential": None} if isinstance(row.payload, dict) else {}
        row.state, row.receipt = state, {"status": status}
        row.save(update_fields=["payload", "state", "receipt", "updated_at"])


def _parent(job, *, state, status, now, updates=None):
    """Stop/wait only the actual still-provisional original root under its org lock."""
    from .website_models import WebsiteConnectionOperation
    plan = authority.historical(job)
    root = WebsiteConnectionOperation.objects.select_for_update().filter(pk=plan["root_operation_id"], action="cms-setup",
        connection_id=job.connection_id, generation=job.generation, state__in=authority.setup.ACTIVE).first()
    if root is None or root.receipt.get("status") not in {"initial_connecting", "initial_access_required"}: return None
    if canonical_bytes(authority.setup.historical(root)) != canonical_bytes(plan["native_intent"]): raise authority._deny()
    selected = native_budget.validate_at(root.receipt.get("native_budget"), started_at=plan["accepted_at"], operation_id=str(root.pk), now=now)
    selected = budget.update_owner_wait(selected, waiting=state == "needs_user", now=now)
    root.state, root.next_attempt_at = state, None
    root.receipt = {**root.receipt, **deepcopy(updates or {}), "status": status, "native_budget": selected}
    if state != "needs_user": root.receipt.pop("user_action", None)
    root.save(update_fields=["receipt", "state", "next_attempt_at", "updated_at"])
    if state in refunds.billing.records.REFUND_TERMINAL:
        try: refunds.reserve_refund(root, now=now)
        except WebsiteAuthorityError:
            root.receipt = {**root.receipt, "financial_settlement": "verification_required"}
            root.save(update_fields=["receipt", "updated_at"])
    return root


def _cancel(job, *, now):
    _wipe(job, state="cancelled", status="authority_revoked")
    try: _parent(job, state="cancelled", status="initial_authority_changed", now=now)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError): pass
    return _job(job, state="cancelled", status="authority_revoked", now=now)


def expire(job, *, now):
    """Erase a proposal after its own deadline without renewing the original plan."""
    value = authority.scope(job, now=now)
    selected = authority.proposal(value, require_key=False, now=now)
    if authority.grants._time(selected.grant["expires_at"]) > now: return False
    _wipe(job, state="needs_user", status="proposal_expired")
    failure = classify_failure("missing_or_expired_credentials", "cms_initial_expiry")
    occurrence = f"cms-initial-expiry:{selected.row.pk}"
    _parent(job, state="needs_user", status="initial_access_required", now=now,
        updates={"failure": failure, "failure_occurrence_id": occurrence, "user_action": {"id": "connect_cms"}})
    _job(job, state="needs_user", status="proposal_expired", now=now, receipt={"failure": failure})
    return True


def _account(value, *, now, active_seconds=0, provider_seconds=0):
    reason = budget.exhaustion(value.budget, now=now, action="cms-initial-connect")
    if reason is not None: raise InitialBudgetStop(reason)
    selected = native_budget.reserve_access_usage(value.root.receipt.get("native_budget"), started_at=value.plan["accepted_at"],
        operation_id=str(value.root.pk), access_operation_id=str(value.job.pk), active_seconds=active_seconds, provider_seconds=provider_seconds, now=now)
    reason = budget.exhaustion(selected, now=now, action="cms-initial-connect")
    if reason is not None: raise InitialBudgetStop(reason)
    value.root.receipt = {**value.root.receipt, "native_budget": selected}
    value.root.save(update_fields=["receipt", "updated_at"])


def _guard(dispatch):
    from .website_models import WebsiteConnectionOperation
    if not isinstance(dispatch, authority.CmsInitialDispatch): raise authority._deny()
    job = WebsiteConnectionOperation.objects.filter(pk=dispatch.operation_id, action=authority.ACTION).first()
    if job is None: raise authority._deny()
    return authority.current(job, dispatch=dispatch)


def _read(dispatch, binding, credential, **options):
    from organizations.models import Organization
    value, proposal = _guard(dispatch)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=value.connection.organization_id)
        value, proposal = _guard(dispatch)
        _account(value, now=timezone.now(), provider_seconds=LEASE_SECONDS)
    value, proposal = _guard(dispatch)
    if binding != value.binding or credential is not None and credential != proposal.credential: raise authority._deny()
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    reply = request_json(binding, None if credential is None else proposal.credential, **options)
    _guard(dispatch)
    return reply


def _stop(job, *, now, reason, updates=None):
    _wipe(job, state="cancelled", status="initial_stopped")
    _parent(job, state="needs_attention", status="initial_verification_stopped", now=now,
        updates={"budget_stop_reason": reason, **(updates or {})})
    return _job(job, state="needs_attention", status="initial_verification_stopped", now=now, receipt={"budget_stop_reason": reason, **(updates or {})})


def _failure(candidate, *, lease, now, error=None, lost=False):
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
        job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=candidate.pk, action=authority.ACTION,
            state="running", payload__lease_id=lease).first()
        if job is None: return None
        try:
            if expire(job, now=now): return job
            value = authority.scope(job, now=now)
        except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError): return _cancel(job, now=now)
        if not authority.enabled(job.connection.organization_id): return _job(job, state="pending", status="initial_paused", now=now, delay=300)
        if isinstance(error, InitialBudgetStop): return _stop(job, now=now, reason=error.reason)
        lost = lost or job.next_attempt_at is None or job.next_attempt_at <= now
        transient = lost or isinstance(error, CmsApiError) and error.category in {"transient", "origin_unavailable"}
        needs_owner = isinstance(error, CmsApiError) and error.category == "credentials_required"
        if isinstance(error, WebsiteAuthorityError) and not error.retryable and not lost: return _cancel(job, now=now)
        transient = transient or isinstance(error, WebsiteAuthorityError) and error.retryable
        failure = classify_failure("RUN_STALLED_WORKER_LOST" if lost else "CONNECTION_SERVICE_TRANSIENT" if transient else
            "missing_or_expired_credentials" if needs_owner else "PUBLISHING_ADAPTER_REQUIRED", "cms_initial_controller")
        occurrence = f"cms-initial:{job.pk}:{job.attempts}:{job.payload['grant_operation_id']}"
        selected, effective, fresh = budget.record_policy_occurrence(value.budget, occurrence=occurrence,
            fingerprint=failure["failure_fingerprint"], classification=failure["failure_class"])
        if fresh: selected["attempts_by_class"][failure["failure_class"]] += 1
        value.root.receipt = {**value.root.receipt, "native_budget": selected}
        value.root.save(update_fields=["receipt", "updated_at"])
        updates = {"failure": failure, "failure_occurrence_id": occurrence, "effective_failure_class": effective}
        reason = budget.exhaustion(selected, now=now, action="cms-initial-connect")
        if reason is not None or not needs_owner and (not transient or effective not in {"transient", "repairable"} or job.attempts >= MAX_ATTEMPTS):
            return _stop(job, now=now, reason=reason or "initial_failure_policy", updates=updates)
        job.payload = {**job.payload, "failures": min(100, job.payload["failures"] + 1)}
        if needs_owner:
            _wipe(job, state="needs_user", status="credential_required")
            _parent(job, state="needs_user", status="initial_access_required", now=now, updates={**updates, "user_action": {"id": "connect_cms"}})
        return _job(job, state="needs_user" if needs_owner else "pending", status="credential_required" if needs_owner else "retrying_initial_access",
            now=now, receipt=updates, delay=min(300, 15 * 2 ** min(job.payload["failures"], 5)))


def advance(identifier, *, now=None):
    """Verify one original grant and admit the same held setup after real GETs."""
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
    if candidate is None or candidate.state not in authority.ACTIVE: return candidate
    now, lease, claimed, attempt = now or timezone.now(), str(uuid4()), False, candidate.attempts
    observed_payload = deepcopy(candidate.payload)
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
            if job is None or job.state not in authority.ACTIVE: return job
            attempt = job.attempts
            observed_payload = deepcopy(job.payload)
            if expire(job, now=now): return job
            if not authority.enabled(job.connection.organization_id) or job.next_attempt_at and job.next_attempt_at > now: return job
            if job.state == "running": return _failure(job, lease=job.payload.get("lease_id"), now=now, lost=True)
            value = authority.scope(job, now=now)
            authority.proposal(value, now=now)
            if job.attempts >= MAX_ATTEMPTS: return _stop(job, now=now, reason="initial_attempts")
            try: _account(value, now=now, active_seconds=LEASE_SECONDS)
            except InitialBudgetStop as error: return _stop(job, now=now, reason=error.reason)
            job.state, job.next_attempt_at, job.attempts = "running", now + timedelta(seconds=LEASE_SECONDS), job.attempts + 1
            job.payload = {**job.payload, "lease_id": lease, "lease_grant_id": job.payload["grant_operation_id"]}
            job.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
        claimed = True
        dispatch = authority.CmsInitialDispatch(str(job.pk), lease, job.payload["grant_operation_id"])
        value, proposal = _guard(dispatch)
        identity = provider.inspect_identity(value.binding, proposal.credential, read=lambda binding, credential, **options: _read(dispatch, binding, credential, **options))
        if identity.get("can_edit_posts") is not True or identity.get("can_publish_posts") is not True: raise CmsApiError("credentials_required")
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=job.connection.organization_id)
            value, proposal = _guard(dispatch)
            value.private.payload = {"schema_version": 1, "binding": value.binding, "sealed_credential": seal_credential(value.binding,
                username=proposal.credential.username, secret=proposal.credential.secret)}
            value.private.state = "completed"
            value.private.save(update_fields=["payload", "state", "updated_at"])
            value.connection.state = "connected"
            value.connection.capabilities = {**value.connection.capabilities, "_cms_identity": identity, "_cms_connect_request_id": proposal.grant["request_id"]}
            value.connection.save(update_fields=["state", "capabilities", "updated_at"])
            value.config.article_system = {**value.config.article_system, "publishing_channel": {**value.record,
                "scope": scope_for(value.context, value.config), "revision": value.record["revision"] + 1}}
            value.config.save(update_fields=["article_system", "updated_at"])
            WebsiteConnectionOperation.objects.create(connection=value.connection, generation=job.generation, action="cms-connect", state="completed",
                idempotency_key=f"cms-connect:{value.binding['organization_id']}:{value.binding['company_id']}:{proposal.grant['request_id']}",
                payload={}, receipt={"status": "connected", "channel": "cms", "platform": value.binding["platform"], "credential_stored": True, "publishing_verified": False})
            selected = budget.update_owner_wait(value.budget, waiting=False, now=timezone.now())
            value.root.state, value.root.next_attempt_at = "pending", timezone.now()
            value.root.receipt = {**value.root.receipt, "native_budget": selected, "status": "starting", "current_step": "discover_native"}
            for name in ("failure", "failure_occurrence_id", "effective_failure_class", "user_action"): value.root.receipt.pop(name, None)
            value.root.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            authority.setup.current(value.root)
            _wipe(job, state="completed", status="identity_verified")
            return _job(job, state="completed", status="identity_verified", now=timezone.now(), receipt={"verified_at": timezone.now().isoformat()})
    except (WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        if claimed: return _failure(job, lease=lease, now=max(now, timezone.now()), error=error)
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION,
                state__in=authority.ACTIVE, attempts=attempt).first()
            return _cancel(job, now=now) if job is not None and job.payload == observed_payload else None


def for_root(root, *, now=None):
    """A provisional root never falls through to normal connected-key discovery."""
    from .website_models import WebsiteConnectionOperation
    job = WebsiteConnectionOperation.objects.filter(pk=(root.connection.capabilities or {}).get("_cms_initial_operation_id"),
        action=authority.ACTION, connection_id=root.connection_id, generation=root.generation).first()
    if job is not None: advance(job.pk, now=now)
    root.refresh_from_db()
    return root


def cancel_for_connection(connection):
    """Erase pending grants before lifecycle changes keys or generations."""
    from .website_models import WebsiteConnectionOperation
    for job in WebsiteConnectionOperation.objects.select_for_update().filter(connection_id=connection.pk, action=authority.ACTION):
        _wipe(job, state="cancelled", status="authority_revoked")
        if job.state in {*authority.ACTIVE, "needs_user"}: _job(job, state="cancelled", status="authority_revoked", now=timezone.now())
