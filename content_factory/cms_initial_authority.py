"""Original provisional-site authority, separate from a connected CMS key."""

from dataclasses import dataclass, field
from types import SimpleNamespace

from django.conf import settings
from django.utils import timezone

from . import cms_onboarding_plan as plans
from . import cms_initial_contract as grants
from . import cms_setup_authority as setup
from . import cms_financial_holds as holds
from . import cms_first_article_subject as subjects
from . import cms_goal_budget as budget
from .cms_access_contract import wait_epoch
from .cms_owner_connection import _owner
from .cms_connection_contract import open_credential
from .channel_artifact_contract import canonical_bytes, digest
from .website_publishing_channels import saved_record, scope_for
from .website_contract import WebsiteAuthorityError

ACTION = "cms-initial-connect"
GRANT_ACTION = "cms-initial-grant"
ACTIVE = frozenset({"pending", "running"})
FIELDS = {"schema_version", "plan", "grant_operation_id", "failures"}


@dataclass(frozen=True)
class CmsInitialDispatch:
    """A server-created lease and original proposal, never public JSON."""

    operation_id: str
    lease_id: str = field(repr=False)
    grant_operation_id: str


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original initial CMS owner, key or setup changed.")


def enabled(organization_id):
    """Initial verification requires independent rollout plus native enrollment."""
    return bool(getattr(settings, "ARTICLES_CMS_ONBOARDING_ENABLED", False) and setup.enabled(organization_id))


def key(plan):
    """One original initial command per actual owner/site acceptance nonce."""
    b = plan["binding"]
    return f"cms-onboarding:{b['organization_id']}:{b['company_id']}:{plan['native_intent']['request_id']}"


def historical(job):
    """Validate saved metadata without opening a proposal or inventing a lease."""
    try:
        p = job.payload
        if (job.action != ACTION or not isinstance(p, dict) or set(p) - {"lease_id", "lease_grant_id"} != FIELDS or
                type(p["schema_version"]) is not int or p["schema_version"] != 1 or
                type(p["failures"]) is not int or not 0 <= p["failures"] <= 100 or
                ("lease_id" in p) != ("lease_grant_id" in p)):
            raise _deny()
        plan = plans.validate(p["plan"])
        grants.uuid(p["grant_operation_id"])
        for name in ("lease_id", "lease_grant_id"):
            if name in p: grants.uuid(p[name])
        if (str(job.pk) != plan["operation_id"] or str(job.connection_id) != plan["binding"]["connection_id"] or
                job.generation != plan["binding"]["generation"] or job.idempotency_key != key(plan)):
            raise _deny()
        return plan
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def scope(job, *, user=None, pending=True, read_only=False, now=None):
    """Reload actual current ownership and original records before reading a key."""
    from .website_models import WebsiteConnectionOperation
    from .models import OrganizationContentConfig
    from founder_tools.models import VibeRaisingCompany
    from django.contrib.auth import get_user_model
    from .dispatch_models import ContentFactoryDispatchOutbox
    from . import website_first_article_billing as records
    from roo.models import Ledger
    try:
        now = now or timezone.now()
        plan = historical(job); binding = plan["binding"]
        config = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(
            pk=plan["offer"]["scope"]["config_id"], organization_id=binding["organization_id"], website_connection_id=job.connection_id).first()
        company = VibeRaisingCompany.objects.select_related("profile").filter(pk=binding["company_id"], organization_id=binding["organization_id"]).first()
        actor = get_user_model().objects.filter(pk=binding["owner_user_id"], is_active=True).first()
        if config is None or company is None or actor is None or user is not None and str(user.pk) != str(actor.pk): raise _deny()
        conn = config.website_connection
        context = SimpleNamespace(company=company, profile=company.profile, organization=conn.organization)
        actual = _owner(context, config, actor)
        expected_scope = {**plan["offer"]["scope"], "connection_id": binding["connection_id"], "generation": binding["generation"], "connection_state": conn.state}
        if (conn.generation != binding["generation"] or str(conn.pk) != binding["connection_id"] or str(conn.authorized_by_id) != binding["owner_user_id"] or
                actual != expected_scope or
                conn.site_url != binding["public_origin"] or any(getattr(conn, name) != "" for name in ("github_repo", "installation_id", "branch", "app_root")) or
                conn.repository_id is not None or config.github_repo != "" or conn.capabilities.get("_cms_binding") != binding or
                conn.capabilities.get("_cms_initial_operation_id") != str(job.pk) or
                conn.capabilities.get("_cms_credential_operation_id") != plan["credential_operation_id"]): raise _deny()
        child = WebsiteConnectionOperation.objects.filter(idempotency_key="cms-setup-intent:" + plan["root_operation_id"],
            action=setup.INTENT_ACTION, state="completed", connection_id=conn.pk, generation=conn.generation).first()
        root = WebsiteConnectionOperation.objects.filter(pk=plan["root_operation_id"], connection_id=conn.pk, generation=conn.generation).first()
        private = WebsiteConnectionOperation.objects.filter(pk=plan["credential_operation_id"], connection_id=conn.pk,
            generation=conn.generation, action="channel-credential").first()
        if root is None or child is None or private is None or canonical_bytes(child.payload) != canonical_bytes(plan["native_intent"]): raise _deny()
        record = saved_record(config.article_system)
        if record is None or record["scope"] != actual or not record["selection"] or record["selection"]["channel"] != "cms" or record["selection"]["platform"] not in {binding["platform"], "unknown"}:
            raise _deny()
        if pending:
            if (conn.state != "paused" or conn.configuration_version != 1 or record["revision"] != plan["offer"]["channel_revision"] + 1 or
                    root.action != setup.ACTION or not read_only and (root.state not in setup.ACTIVE or root.receipt.get("status") not in {"initial_connecting", "initial_access_required"}) or
                    canonical_bytes(setup.historical(root)) != canonical_bytes(plan["native_intent"]) or private.state != "pending" or
                    private.payload != {"schema_version": 1, "binding": binding}): raise _deny()
            subject = subjects.from_setup_intent(plan["native_intent"], expected_binding=binding)
            reservation = holds.reservation(root, subject)
            if not read_only:
                if reservation["status"] != "reserved" or Ledger.objects.filter(idempotency_key="content_factory:refund:" + reservation["client_request_id"]).exists(): raise _deny()
                spend = records._original_spend(reservation)
                payer = spend.user if spend is not None else get_user_model().objects.filter(pk=reservation["payer_user_id"], is_active=True).first()
                if payer is None or payer.is_active is not True or ContentFactoryDispatchOutbox.objects.filter(client_request_id=reservation["client_request_id"]).exists(): raise _deny()
            selected = budget.validate_at(root.receipt.get("native_budget"), started_at=plan["accepted_at"], operation_id=str(root.pk), now=now)
        else:
            if job.state != "completed" or conn.state != "connected": raise _deny()
            from .cms_connection_authority import current_credential
            if current_credential(context, config, conn, user=actor)[0] != binding: raise _deny()
            selected = None
        return SimpleNamespace(job=job, plan=plan, binding=binding, config=config, context=context, connection=conn,
            user=actor, root=root, private=private, record=record, budget=selected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def proposal(value, *, require_key=True, now=None):
    """Select the actual current private proposal; failed grants cannot revive."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    row = WebsiteConnectionOperation.objects.filter(pk=value.job.payload["grant_operation_id"], action=GRANT_ACTION,
        connection_id=value.job.connection_id, generation=value.job.generation).first()
    try:
        p = row.payload
        if (not isinstance(p, dict) or set(p) != {"schema_version", "grant", "sealed_credential"} or type(p["schema_version"]) is not int or p["schema_version"] != 1): raise _deny()
        grant = grants.validate(p["grant"], plan=value.plan)
        if (str(row.pk) != grant["operation_id"] or row.idempotency_key != f"cms-initial-grant:{value.job.pk}:{grant['request_id']}"):
            raise _deny()
        if value.job.state == "completed":
            if (row.state != "completed" or p["sealed_credential"] is not None or row.receipt != {"status": "identity_verified"} or
                    value.job.receipt.get("status") != "identity_verified" or
                    not grants._time(grant["accepted_at"]) <= grants._time(value.job.receipt.get("verified_at")) <= now):
                raise _deny()
        elif value.job.state in ACTIVE:
            if row.state != "pending" or not isinstance(p["sealed_credential"], str) or digest(p["sealed_credential"]) != grant["proposal_digest"]:
                raise _deny()
        elif p["sealed_credential"] is not None or row.state not in {"cancelled", "needs_user"}:
            raise _deny()
        if require_key and (row.state != "pending" or value.job.state not in ACTIVE or
                not grants._time(grant["accepted_at"]) <= now < grants._time(grant["expires_at"]) or
                not isinstance(p["sealed_credential"], str) or digest(p["sealed_credential"]) != grant["proposal_digest"] or
                grant["access_epoch"] != wait_epoch(value.root)):
            raise _deny()
        credential = open_credential(p["sealed_credential"], binding=value.binding) if require_key else None
        return SimpleNamespace(row=row, grant=grant, credential=credential)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError): raise _deny() from None


def current(job, *, dispatch, now=None):
    """Only the enabled winning private lease can open a pending encrypted key."""
    now = now or timezone.now()
    if (not isinstance(dispatch, CmsInitialDispatch) or str(job.pk) != dispatch.operation_id or not enabled(job.connection.organization_id) or
            job.state != "running" or job.payload.get("lease_id") != dispatch.lease_id or
            job.payload.get("lease_grant_id") != dispatch.grant_operation_id or job.payload.get("grant_operation_id") != dispatch.grant_operation_id or
            job.next_attempt_at is None or job.next_attempt_at <= now): raise _deny()
    value = scope(job, now=now)
    return value, proposal(value, now=now)
