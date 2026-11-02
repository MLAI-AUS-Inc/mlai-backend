"""Keep owned no-start obligations held without an exact withdrawal scope."""
from django.db.models import Q


def no_start_refund_allowed(*, user, article_request, resolved_domain):
    """Legacy controllers keep their behavior; admitted charges need closure.

    Resolve the immutable logical key and the retained original debit, rather
    than trusting a mutable Job billing flag or an incoming worker status.
    This check grants no authority: the private settlement scope independently
    rechecks the exact original payer, key, charge, tenant and current ticket.
    """
    from workflow_runs.models import ContentFactoryRun
    from roo.models import Ledger
    from .run_reconciliation import authoritative_run
    if not isinstance(article_request, dict):
        return False
    key = article_request.get("client_request_id")
    lookup = Q(result__run_owner_v1__identity__dispatch_key=key) | Q(
        result__run_owner_article_charge_v1__article_request_key=key)
    ledger_ids = list(Ledger.objects.filter(kind="SPEND", source="CONTENT_FACTORY",
        idempotency_key__in=["content_factory:charge:" + str(key),
                             "content_factory:topic_generation:charge:" + str(key)]).values_list("pk", flat=True))
    if article_request.get("roo_points_ledger_id") is not None:
        try:
            ledger_ids.append(int(article_request["roo_points_ledger_id"]))
        except (TypeError, ValueError, OverflowError):
            return False
    for ledger_id in ledger_ids:
        lookup |= (Q(billing_ledger_id=ledger_id) | Q(run_request__roo_points_ledger_id=str(ledger_id))
                   | Q(run_request__roo_points_ledger_id=ledger_id)
                   | Q(result__run_owner_article_charge_v1__ledger_id=ledger_id))
    owned = ContentFactoryRun.objects.filter(lookup).filter(result__run_owner_v1__isnull=False)
    if not any(authoritative_run(run) for run in owned.iterator()):
        return True
    try:
        from .run_dispatch_settlement import refund_authority_matches
    except ImportError:
        return False
    return refund_authority_matches(user=user, article_request=article_request, resolved_domain=resolved_domain)
