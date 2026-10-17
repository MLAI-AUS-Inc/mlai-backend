"""Original managed-goal permission for new work, separate from read recovery."""

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_budget as budget
from .channel_artifact_contract import digest, validate_envelope, ArtifactContractError
from .website_contract import WebsiteAuthorityError

FIELDS = ("cms_goal_operation_id", "cms_goal_id")
WORK_STATES = frozenset({"pending", "running"})


class CmsGoalWorkDenied(WebsiteAuthorityError):
    """New work is stopped; an issued provider effect still needs read recovery."""

    def __init__(self):
        super().__init__("website_operation_changed", "The original native articles goal cannot start more work.")


def _deny():
    return CmsGoalWorkDenied()


def require_work(payload, *, now=None, action=None):
    """Reload original dispatch, private current goal, held money and its budget.

    Call only for new model work or immediately before reserving/using a POST
    allowance. Existing GET-only reconciliation intentionally does not use this
    boundary: a stopped goal must still resolve an already-issued resource.
    Ordinary paid CMS articles without managed-goal metadata are unchanged.
    """
    if isinstance(payload, dict) and not any(name in payload for name in FIELDS) and not str(payload.get("client_request_id") or "").startswith("cms-articles-first:"):
        return None
    try:
        envelope = validate_envelope(payload.get("publishing_channel_artifact"))
        from organizations.models import Organization
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=envelope["organization_id"])
            return _require_work(payload, now=now or timezone.now(), action=action)
    except (WebsiteAuthorityError, ArtifactContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def _require_work(payload, *, now, action):
    try:
        key = payload.get("client_request_id")
        identifier = payload.get("cms_goal_operation_id")
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise _deny()
        from .dispatch_models import ContentFactoryDispatchOutbox
        from .website_models import WebsiteConnectionOperation
        from roo.models import Ledger
        row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).first()
        original = row.payload.get("request") if row and isinstance(row.payload, dict) else None
        if (not isinstance(original, dict) or row.state in {"failed", "cancelled", "refund_pending"} or
                any(original.get(name) != payload.get(name) for name in (*FIELDS, "client_request_id", "publishing_channel_artifact"))):
            raise _deny()
        operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=authority.ACTION).first()
        if operation is None:
            raise _deny()
        value = authority.current(operation, states=WORK_STATES)
        latest = operation.connection.operations.filter(action=authority.ACTION, generation=operation.generation).order_by("-created_at", "-pk").first()
        if latest is None or latest.pk != operation.pk:
            raise _deny()
        from .channel_artifacts import authorize_payload
        envelope = authorize_payload(original).envelope
        held = billing.historical_reservation(operation)
        if (payload.get("cms_goal_id") != value.goal["goal_id"] or key != billing.request_key(value) or
                str(row.organization_id) != value.scope["binding"]["organization_id"] or
                envelope["scope_sha256"] != digest(value.scope["binding"]) or envelope["billing_request_id"] != key or
                held["status"] == "refunded" or str(original.get("roo_points_billing_user_id")) != held["payer_user_id"] or
                original.get("roo_points_cost") != held["cost_points"] or
                original.get("roo_points_ledger_id") != held["spend_ledger_id"] or
                Ledger.objects.filter(idempotency_key="content_factory:refund:" + key).exists()):
            raise _deny()
        ledger = billing.records._original_spend(held)
        if ledger is not None and (ledger.user is None or ledger.user.is_active is not True):
            raise _deny()
        if budget.exhaustion(operation, now=now, action=action) is not None:
            raise _deny()
        return operation
    except WebsiteAuthorityError:
        raise
    except (ArtifactContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
