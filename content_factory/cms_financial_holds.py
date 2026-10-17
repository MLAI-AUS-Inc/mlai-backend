"""Original native article money records, without supplying provider authority."""

from django.db import transaction

from . import website_first_article_billing as records
from .channel_artifact_contract import canonical_bytes
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native article payment could not be verified.")


def reservation(operation, subject):
    """Read the exact original subject; a receipt is not current work permission."""
    receipt = records._reservation(operation, subject)
    if (canonical_bytes(receipt["binding"]) != canonical_bytes(records._binding(subject)) or
            len(receipt["payer_user_id"]) > 19 or not 0 < int(receipt["payer_user_id"]) < 2**63):
        raise _deny()
    return receipt


def reserve(operation, subject, *, context, connection, user, now):
    """Hold the current accepted price inside the authenticated owner's transaction.

    Callers independently validate their actual setup or goal authority. This
    internal monetary helper neither opens a provider lease nor accepts an HTTP
    subject selector. An existing exact receipt keeps its original price.
    """
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    if operation.receipt.get("first_article_billing") is not None:
        receipt = reservation(operation, subject)
    else:
        from .billing import get_content_factory_article_cost_points
        from .company_billing import billing_user_for_organization
        from founder_tools.services import founder_actor_id_for_user
        from roo.permissions import InsufficientBalanceError, IdempotencyConflictError
        from roo.services import PointsService
        cost = subject["consent"]["answers"]["billing"]["expected_points"]
        if cost != get_content_factory_article_cost_points(context.organization.domain):
            raise WebsiteAuthorityError("roo_points_required", "Review the current first-article price before starting.", status=409)
        payer = billing_user_for_organization(context.organization, user)
        key, ledger = records.request_key(subject), None
        if cost:
            try:
                ledger, _created = PointsService.spend(user=payer, delta=cost, source="CONTENT_FACTORY",
                    description=f"Reserved first native CMS article for {context.organization.domain} (requested by account {user.pk})",
                    created_by_slack_id=founder_actor_id_for_user(user), idempotency_key="content_factory:charge:" + key,
                    reference_type="CONTENT_FACTORY", reference_id=key)
            except InsufficientBalanceError:
                raise WebsiteAuthorityError("roo_points_required", "Add Roo points to reserve the first article.", status=402) from None
            except IdempotencyConflictError:
                raise _deny() from None
        receipt = records._save(operation, {"version": 1, "binding": records._binding(subject), "client_request_id": key,
            "payer_user_id": str(payer.pk), "requester_user_id": str(user.pk), "cost_points": cost,
            "spend_ledger_id": str(ledger.pk) if ledger else None, "reserved_at": now.isoformat(), "status": "reserved"})
    if connection.capabilities.get("_cms_financial_records") is not True:
        connection.capabilities = {**connection.capabilities, "_cms_financial_records": True}
        connection.save(update_fields=["capabilities", "updated_at"])
    return receipt
