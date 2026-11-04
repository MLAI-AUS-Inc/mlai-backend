"""Read-only founder command offers; mutation endpoints retain fresh authority."""
from copy import deepcopy


def _disabled():
    return {"canCancel": False, "canDeny": False, "cancelCopy": None, "denyCopy": None, "cancelOutcome": None}


def _stopped_or_settling(run):
    from .run_ownership import RECONCILIATION_RESULT_KEY, user_stop_intent
    from .run_terminal_settlement import TERMINAL_CLOSURES_KEY, TERMINAL_SETTLEMENT_KEY, terminal_execution_closed
    from .run_dispatch_settlement import dispatch_closed_without_effects
    from .no_delivery_refunds import points_receipt
    from .founder_journey_contract import effective_approval_state
    result = run.result
    if not isinstance(result, dict):
        return True
    if (user_stop_intent(run) or result.get("cancellation_reason") or effective_approval_state(run) == "denied"
            or terminal_execution_closed(run) or dispatch_closed_without_effects(run)
            or "article_terminal_refund" in result or result.get("reconciliation_refund_pending")
            or points_receipt(run).get("state") in {"refunded", "released", "kept"}):
        return True
    if RECONCILIATION_RESULT_KEY in result:
        meta = result[RECONCILIATION_RESULT_KEY]
        if not isinstance(meta, dict) or meta.get("terminal_claim") or meta.get(TERMINAL_CLOSURES_KEY) or meta.get(TERMINAL_SETTLEMENT_KEY):
            return True
    return False


def article_controls(*, organization, company, user, run):
    """Offer only owner-scoped local commands, without reads from a worker.

    These booleans and conditional points copy are presentation hints. They do
    not withdraw consent, stop a worker, promise a refund or settle a charge.
    Every command rechecks its original current scope and permission.
    """
    from django.contrib.auth import get_user_model
    from founder_tools.models import VibeRaisingCompany
    from workflow_runs.models import ContentFactoryRun
    from .models import OrganizationContentConfig
    from .run_state import ARTICLE_WORKFLOWS
    from .article_publication_evidence import publication_state
    from .founder_journey_contract import _rights
    from .no_delivery_refunds import cancel_outcome, _delivery
    empty = _disabled()
    if run is None or user is None or organization is None or company is None:
        return empty
    fresh_user = get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).first()
    config = OrganizationContentConfig.objects.filter(organization_id=organization.pk).first()
    current = ContentFactoryRun.objects.filter(pk=run.pk, organization_id=organization.pk).first()
    if (fresh_user is None or config is None or current is None or current.workflow not in ARTICLE_WORKFLOWS
            or current.run_id != run.run_id or current.domain != organization.domain
            or not VibeRaisingCompany.objects.filter(pk=company.pk, organization_id=organization.pk,
                profile__user_id=fresh_user.pk).exists()):
        return empty
    _, writable, _, owner = _rights(organization, company, config, fresh_user)
    if not owner or not writable:
        return empty
    try:
        if (current.status in {"completed", "cancelled", "denied"} or _stopped_or_settling(current)
                or publication_state(current) != "none"):
            return empty
        outcome = cancel_outcome(current)
        if (not isinstance(outcome, dict) or set(outcome) != {"state", "points", "freeArticle"}
                or outcome["state"] not in {"no_charge", "refund", "kept"} or type(outcome["freeArticle"]) is not bool
                or outcome["points"] is not None and (type(outcome["points"]) is not int or outcome["points"] < 0)):
            return empty
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return empty
    if outcome["freeArticle"]:
        body = "Your saved draft stays available. We'll confirm whether your free article can be used again after the stop."
    elif outcome["state"] == "refund":
        amount = f"{outcome['points']} points" if outcome["points"] is not None else "Your points"
        body = f"{amount} may be returned if no draft has been written and no publishing has started. We'll confirm this after the stop."
    elif outcome["state"] == "kept":
        from .website_run_authority import event_owned
        body = ("The points stay held until the stop and any delivered value are confirmed. Your saved draft stays available."
            if event_owned(current) and not _delivery(current) else
            "Writing has started, so the points stay with this article. Your saved draft stays available.")
    else:
        body = "We'll stop this article. Your saved draft stays available."
    deny = current.status in {"awaiting_approval", "approval_required"}
    return {"canCancel": True, "canDeny": deny,
        "cancelCopy": {"headline": "Stop this article?", "body": body, "button": "Stop article"},
        "denyCopy": {"headline": "Withdraw approval?", "body": "Your saved draft stays available. We'll stop work and confirm any points outcome after the stop.",
            "button": "Withdraw approval"} if deny else None,
        "cancelOutcome": deepcopy(outcome)}
