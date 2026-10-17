"""Pre-discovery holds and unused credits for actual private setup operations."""

from django.db import transaction

from . import cms_setup_authority as authority
from . import cms_first_article_subject as subjects
from . import cms_financial_holds as holds
from . import website_first_article_billing as records
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native setup payment could not be verified.")


def historical_reservation(operation):
    """Read original setup money after cancellation without granting work permission."""
    original = authority.historical(operation)
    subject = subjects.from_setup_intent(original, expected_binding=original["binding"])
    return holds.reservation(operation, subject)


def reserve(operation, *, user, now):
    """Accept the original current price before the first provider discovery read."""
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    value = authority.current(operation, user=user)
    if operation.receipt.get("first_article_billing") is None and operation.state != "pending":
        raise _deny()
    subject = subjects.from_setup_intent(value.intent, expected_binding=value.binding)
    return holds.reserve(operation, subject, context=value.context, connection=value.connection, user=user, now=now)


def require_held(operation):
    """Verify the real original uncredited spend inside a short owning transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    receipt = historical_reservation(operation)
    from roo.models import Ledger
    if receipt["status"] != "reserved" or Ledger.objects.filter(idempotency_key="content_factory:refund:" + receipt["client_request_id"]).exists():
        raise _deny()
    ledger = records._original_spend(receipt)
    from django.contrib.auth import get_user_model
    payer = ledger.user if ledger else get_user_model().objects.filter(pk=receipt["payer_user_id"], is_active=True).first()
    if payer is None or payer.is_active is not True:
        raise _deny()
    return receipt


def release(operation, *, reason):
    """Credit an unused setup hold, independently of rollout or credential state.

    This action can only discover native defaults. Under the organization lock,
    a setup row cannot also be promoted/dispatched, and any actual outbox for
    its original payment prevents this unused-hold credit.
    """
    if not transaction.get_connection().in_atomic_block or operation.state not in records.REFUND_TERMINAL:
        raise _deny()
    from organizations.models import Organization
    Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
    receipt = historical_reservation(operation)
    from .dispatch_models import ContentFactoryDispatchOutbox
    if ContentFactoryDispatchOutbox.objects.filter(client_request_id=receipt["client_request_id"]).exists():
        raise _deny()
    ledger = records._original_spend(receipt, allow_refunded=True)
    from roo.models import Ledger
    rows = Ledger.objects.filter(idempotency_key="content_factory:refund:" + receipt["client_request_id"])
    credited = rows.first() if rows.exists() else None
    if credited is None and ledger is not None:
        from founder_tools.services import founder_actor_id_for_user
        from integrations.services.article_generation import _refund_content_factory_request
        credited = _refund_content_factory_request(user=ledger.user, slack_user_id=founder_actor_id_for_user(ledger.user),
            article_request={"client_request_id": receipt["client_request_id"], "roo_points_ledger_id": receipt["spend_ledger_id"],
                "roo_points_cost": receipt["cost_points"]}, resolved_domain=operation.connection.organization.domain, reason=reason)
    if receipt["cost_points"]:
        from .cms_goal_billing import _validate_credit
        _validate_credit(credited, receipt)
    return records._save(operation, {**receipt, "status": "refunded"})
