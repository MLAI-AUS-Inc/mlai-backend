"""Safe pending-access projections and a parent barrier, without key export."""

from datetime import timedelta

from django.utils import timezone

from . import cms_access_authority as authority
from . import cms_access_contract as contract
from .cms_access_start import latest
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import validate_failure


def projection(job):
    """Project actual owner grant identity/schedule, never credential health."""
    intent = authority.historical(job)
    try: failure = validate_failure(job.receipt.get("failure"))
    except ValueError: failure = {}
    if job.state == "completed":
        at = contract.goals._time(job.receipt["verified_at"])
        if (job.payload["sealed_credential"] is not None or job.receipt.get("status") != "access_updated" or
                job.receipt.get("intent_digest") != intent["digest"] or
                not contract.goals._time(intent["accepted_at"]) <= at <= contract.goals._time(intent["expires_at"])):
            raise authority._deny()
    if job.state not in {*authority.ACTIVE, "completed", "needs_user", "needs_attention", "cancelled"}:
        raise authority._deny()
    if job.state in authority.ACTIVE and job.next_attempt_at is None: raise authority._deny()
    return {"version": 1, "companyId": intent["binding"]["company_id"], "connectionId": intent["binding"]["connection_id"],
        "connectionGeneration": intent["binding"]["generation"], "operationId": str(job.pk), "rootOperationId": intent["root_operation_id"],
        "requestId": intent["request_id"], "accessEpoch": intent["access_epoch"], "state": job.state,
        "nextAttemptAt": job.next_attempt_at.isoformat() if job.state in authority.ACTIVE and job.next_attempt_at is not None else None,
        "failureCode": failure.get("code")}


def for_root(root):
    """Only the current original root's real wait can select its active child."""
    epoch = contract.wait_epoch(root)
    job = latest(root, epoch=epoch) if epoch is not None else latest(root)
    if job is None: return None
    try:
        intent = authority.historical(job)
        if intent["binding"] != root.payload.get("binding") or intent["root_operation_id"] != str(root.pk): return None
        if job.state in authority.ACTIVE: authority.owner_scope(job)
        return projection(job)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def apply(result, root):
    """Pending verified input is actual work, not another owner credential stop."""
    selected = for_root(root)
    if selected is not None and selected["state"] in authority.ACTIVE:
        if authority.enabled():
            result.update(state="working", title="Checking CMS access", message="Your saved access request will continue after this page closes.",
                action=None, nextAttemptAt=selected["nextAttemptAt"])
        else:
            result.update(state="stopped", title="CMS access check paused", message="The saved access request is paused.", nextAttemptAt=None,
                action={"id": "inspect_goal", "label": "View details", "operationId": str(root.pk)})
    return result


def defer_parent(root, *, now):
    """Under the owning org lock, defer old-key retries without claiming a lease."""
    epoch = contract.wait_epoch(root)
    if epoch is None: return False
    job = latest(root, epoch=epoch)
    if job is None or job.state not in authority.ACTIVE: return False
    from .cms_access_controller import _expire, _write
    try:
        if _expire(job, now=now): return False
        authority.owner_scope(job, now=now)
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _write(job, state="cancelled", status="authority_revoked", now=now, wipe=True)
        return False
    root.next_attempt_at = max(now + timedelta(seconds=15), min(job.next_attempt_at or now, now + timedelta(seconds=300)))
    root.save(update_fields=["next_attempt_at"])
    return True


def expire_due(*, connection_id=None, limit=20, now=None):
    """Bounded independent erasure also runs when the execution flag is off."""
    from organizations.models import Organization
    from django.db import transaction
    from django.db.models import Q
    from .website_models import WebsiteConnectionOperation
    from .cms_access_controller import _expire, _write
    now = now or timezone.now()
    rows = WebsiteConnectionOperation.objects.filter(Q(payload__intent__expires_at__lte=now.isoformat()) |
        Q(created_at__lte=now - contract.TTL), action=authority.ACTION, state__in=authority.ACTIVE)
    if connection_id: rows = rows.filter(connection_id=connection_id)
    for candidate in rows.order_by("created_at")[:limit]:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            job = WebsiteConnectionOperation.objects.select_for_update().filter(pk=candidate.pk, action=authority.ACTION, state__in=authority.ACTIVE).first()
            if job is None: continue
            try: _expire(job, now=now)
            except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                _write(job, state="cancelled", status="authority_revoked", now=now, wipe=True)
