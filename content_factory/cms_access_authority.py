"""Current private credential-job authority is independent of its metadata hash."""

from dataclasses import dataclass, field
from types import SimpleNamespace

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import cms_access_contract as contract
from . import cms_credential_renewal as renewal
from . import cms_goal_budget as budget
from .channel_artifact_contract import digest
from .cms_connection_contract import open_credential
from .website_contract import WebsiteAuthorityError
from .website_publishing_channels import saved_record

ACTION = "cms-credential-renewal"
ACTIVE = frozenset({"pending", "running"})
FIELDS = {"schema_version", "intent", "sealed_credential", "failures"}


@dataclass(frozen=True)
class CmsAccessDispatch:
    """Only the server's current credential worker can open this lease."""

    operation_id: str
    lease_id: str = field(repr=False)


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original CMS access request or owner changed.")


def enabled():
    """Durable access execution is independently disabled until acceptance."""
    return bool(getattr(settings, "ARTICLES_CMS_ACCESS_RENEWAL_ENABLED", False))


def key(intent):
    """Bind an owner retry UUID to its actual site and original root."""
    return f"cms-access:{intent['binding']['connection_id']}:{intent['binding']['generation']}:{intent['root_operation_id']}:{intent['request_id']}"


def historical(job):
    """Validate the closed original journal without reopening an erased key."""
    try:
        payload = job.payload
        if (job.action != ACTION or not isinstance(payload, dict) or set(payload) - {"lease_id"} != FIELDS or
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
                type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 20 or
                payload["sealed_credential"] is not None and (not isinstance(payload["sealed_credential"], str) or len(payload["sealed_credential"]) > 8192)):
            raise _deny()
        intent = contract.validate_intent(payload["intent"])
        if "lease_id" in payload: contract._uuid(payload["lease_id"])
        if (intent["operation_id"] != str(job.pk) or intent["binding"]["connection_id"] != str(job.connection_id) or
                intent["binding"]["generation"] != job.generation or job.idempotency_key != key(intent) or
                payload["sealed_credential"] is not None and digest(payload["sealed_credential"]) != intent["proposal_digest"]):
            raise _deny()
        return intent
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def root_current(root, *, user=None, waiting=True):
    """Reload the real selected native root and its original unrefunded payment."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    if root is None or root.action not in {"cms-goal", "cms-setup"}:
        raise _deny()
    # Status and unlocked provider guards also read this financial identity.
    # Lock the actual root and original spend in a short transaction that ends
    # before the caller can send a credential-bearing request.
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=root.connection.organization_id)
        actual = WebsiteConnectionOperation.objects.select_for_update().filter(pk=root.pk, action=root.action).first()
        if actual is None:
            raise _deny()
        return _root_current(actual, user=user, waiting=waiting)


def _root_current(root, *, user, waiting):
    from . import cms_goal_authority as native
    from . import cms_setup_authority as setup
    from . import cms_goal_billing, cms_setup_billing
    from . import website_first_article_billing as records
    from roo.models import Ledger
    from django.contrib.auth import get_user_model
    if root is None or root.action not in {native.ACTION, setup.ACTION}:
        raise _deny()
    value = (native if root.action == native.ACTION else setup).current(root, user=user, states={root.state})
    binding = value.scope["binding"] if root.action == native.ACTION else value.binding
    if waiting and contract.wait_epoch(root) is None:
        raise _deny()
    original = (cms_goal_billing if root.action == native.ACTION else cms_setup_billing).historical_reservation(root)
    if original["status"] == "refunded" or Ledger.objects.filter(idempotency_key="content_factory:refund:" + original["client_request_id"]).exists():
        raise _deny()
    spend = records._original_spend(original)
    payer = spend.user if spend is not None else get_user_model().objects.filter(pk=original["payer_user_id"], is_active=True).first()
    if payer is None or payer.is_active is not True:
        raise _deny()
    started_at = value.goal["started_at"] if root.action == native.ACTION else value.intent["accepted_at"]
    return SimpleNamespace(**value.__dict__, actual_binding=binding, started_at=started_at)


def owner_scope(job, *, user=None, now=None):
    """Read actual pending ownership without opening a key or enabling work."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    intent = historical(job)
    if (job.state not in ACTIVE or not isinstance(job.payload["sealed_credential"], str) or
            contract.goals._time(intent["accepted_at"]) > now or contract.goals._time(intent["expires_at"]) <= now or
            not job.payload["sealed_credential"].startswith("mlai-enc:v1:")):
        raise _deny()
    root = WebsiteConnectionOperation.objects.filter(pk=intent["root_operation_id"], action=intent["root_action"],
        connection_id=job.connection_id, generation=job.generation).first()
    value = root_current(root, user=user)
    selected = renewal.candidate(value.context, value.config, user=value.user, proposal=value.actual_binding)
    expected = renewal.Renewal(intent["binding"], intent["credential_operation_id"], intent["credential_digest"],
        intent["principal_id"], intent["configuration_revision"])
    if (selected != expected or contract.wait_epoch(root) != intent["access_epoch"] or
            digest(saved_record(value.config.article_system)) != intent["channel_choice_digest"]):
        raise _deny()
    selected_budget = budget.validate_at(root.receipt.get("native_budget"), started_at=value.started_at, operation_id=str(root.pk), now=now)
    return SimpleNamespace(job=job, intent=intent, root=root, value=value, selected=selected, budget=selected_budget)


def current(job, *, lease_id=None, user=None, now=None):
    """Require the current enabled private lease before opening its proposal."""
    now = now or timezone.now()
    if (not enabled() or lease_id is not None and (job.state != "running" or job.payload.get("lease_id") != lease_id or
            job.next_attempt_at is None or job.next_attempt_at <= now)):
        raise _deny()
    value = owner_scope(job, user=user, now=now)
    credential = open_credential(job.payload["sealed_credential"], binding=value.intent["binding"])
    return SimpleNamespace(**value.__dict__, credential=credential)
