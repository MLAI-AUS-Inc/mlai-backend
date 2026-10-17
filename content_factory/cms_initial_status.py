"""Owner-only initial status and bounded erasure; GET performs no work."""

from django.conf import settings
from django.utils import timezone

from . import cms_initial_authority as authority
from .cms_access_contract import wait_epoch
from .cms_owner_connection import _request_id
from .website_contract import WebsiteAuthorityError


def find(context, config, *, user, operation_id=None, request_id=None, now=None):
    """Resolve only this selected founder's actual initial command or root."""
    from .website_models import WebsiteConnectionOperation
    try:
        if (operation_id is None) == (request_id is None): raise authority._deny()
        rows = WebsiteConnectionOperation.objects.filter(action=authority.ACTION, connection_id=config.website_connection_id,
            payload__plan__binding__company_id=str(context.company.pk))
        if operation_id is not None:
            authority.grants.uuid(operation_id)
            job = rows.filter(payload__plan__root_operation_id=operation_id).first()
        else:
            request_id = _request_id({"client_request_id": request_id})
            job = rows.filter(idempotency_key=f"cms-onboarding:{context.organization.pk}:{context.company.pk}:{request_id}").first()
        if job is None: raise authority._deny()
        value = authority.scope(job, user=user, pending=job.state != "completed", read_only=True, now=now)
        if str(value.config.pk) != str(config.pk) or str(value.context.company.pk) != str(context.company.pk): raise authority._deny()
        return value
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError): raise authority._deny() from None


def packet(context, config, *, user, operation_id=None, request_id=None, now=None):
    """Read an actual accepted nonce, current destination and real worker schedule."""
    now = now or timezone.now()
    value = find(context, config, user=user, operation_id=operation_id, request_id=request_id, now=now)
    selected = authority.proposal(value, require_key=False, now=now)
    job, root, binding = value.job, value.root, value.binding
    epoch = wait_epoch(root)
    active = job.state in authority.ACTIVE and root.state in authority.setup.ACTIVE
    if active and job.next_attempt_at is None: raise authority._deny()
    descriptor = {"version": 1, "companyId": binding["company_id"], "connectionId": binding["connection_id"], "connectionGeneration": binding["generation"],
        "configurationRevision": value.connection.configuration_version, "operationId": str(root.pk), "stageOperationId": str(job.pk),
        "platform": binding["platform"], "publicOrigin": binding["public_origin"], "apiOrigin": binding["api_origin"],
        "requestId": selected.grant["request_id"], "accessEpoch": epoch, "state": job.state,
        "nextAttemptAt": job.next_attempt_at.isoformat() if active else None,
        "canRestore": bool(job.state == "needs_user" and epoch is not None and authority.enabled(value.connection.organization_id))}
    result = {"cmsInitial": descriptor}
    if job.state != "completed":
        working = active and authority.enabled(value.connection.organization_id)
        result["websiteStatus"] = {"version": 1, "channel": "cms", "mode": "connect", "companyId": binding["company_id"],
            "connectionId": binding["connection_id"], "connectionGeneration": binding["generation"], "configurationRevision": value.connection.configuration_version,
            "operationId": str(root.pk), "goalId": None, "target": "first_article_live", "step": "cms_connect", "expectedTime": None,
            "updatedAt": root.updated_at.isoformat(), "state": "working" if working else "needs_you" if descriptor["canRestore"] else "stopped",
            "title": "Connecting your CMS" if working else "Restore CMS access" if descriptor["canRestore"] else "CMS connection paused",
            "message": "Your saved setup will continue after this page closes." if working else "Enter a valid application or integration credential." if descriptor["canRestore"] else "View the recorded initial setup.",
            "nextAttemptAt": descriptor["nextAttemptAt"] if working else None, "milestones": [], "evidence": [],
            "action": None if working else {"id": "connect_initial_cms" if descriptor["canRestore"] else "inspect_goal",
                "label": "Restore CMS access" if descriptor["canRestore"] else "View details", "operationId": str(root.pk)}}
    again = find(context, config, user=user, operation_id=str(root.pk), now=now)
    if again.binding != binding or again.job.payload != job.payload or again.connection.state != value.connection.state:
        raise authority._deny()
    return result


def for_context(context, config, *, user, now=None):
    """Only an actual provisional connection has an initial rather than native goal."""
    if (not getattr(settings, "ARTICLES_CMS_STATUS_ENABLED", False) or config.website_connection is None or
            config.website_connection.state != "paused" or not config.website_connection.capabilities.get("_cms_initial_operation_id")):
        return None
    from .website_models import WebsiteConnectionOperation
    job = WebsiteConnectionOperation.objects.filter(pk=config.website_connection.capabilities["_cms_initial_operation_id"], action=authority.ACTION,
        connection_id=config.website_connection_id).first()
    if job is None: return None
    try: return packet(context, config, user=user, operation_id=authority.historical(job)["root_operation_id"], now=now)["websiteStatus"]
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError): return None


def expire_due(*, limit=20, connection_id=None, now=None):
    """A key proposal's own 24-hour deadline is enforced even with rollout off."""
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    from django.db import transaction
    from django.db.models import Q
    from .cms_initial_controller import expire, _cancel
    now = now or timezone.now()
    rows = WebsiteConnectionOperation.objects.filter(Q(payload__grant__expires_at__lte=now.isoformat()) | Q(created_at__lte=now - authority.plans.TTL),
        action=authority.GRANT_ACTION, state="pending")
    if connection_id: rows = rows.filter(connection_id=connection_id)
    for grant in rows.order_by("created_at")[:limit]:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=grant.connection.organization_id)
            grant = WebsiteConnectionOperation.objects.select_for_update().filter(pk=grant.pk, action=authority.GRANT_ACTION, state="pending").first()
            if grant is None: continue
            job = WebsiteConnectionOperation.objects.select_for_update().filter(action=authority.ACTION, connection_id=grant.connection_id,
                generation=grant.generation, state__in=authority.ACTIVE, payload__grant_operation_id=str(grant.pk)).first()
            if job is None:
                # Erasure does not need a usable parent, original owner or key.
                # Reload the exact due row under lock so a newer grant is never
                # substituted and a completed grant cannot be rewritten.
                grant.payload = {**grant.payload, "sealed_credential": None} if isinstance(grant.payload, dict) else {}
                grant.state, grant.receipt = "cancelled", {"status": "orphan_proposal_expired"}
                grant.save(update_fields=["payload", "state", "receipt", "updated_at"])
                continue
            try: expire(job, now=now)
            except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError): _cancel(job, now=now)
