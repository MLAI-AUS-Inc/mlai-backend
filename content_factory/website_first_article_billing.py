"""One original, consented first-article spend, retained across goal retries."""

from copy import deepcopy

from django.conf import settings
from django.core.exceptions import ValidationError

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_setup_consent import valid as valid_consent


ACTIVE = {"pending", "running", "needs_user"}
REFUND_TERMINAL = {"cancelled", "denied", "deleted", "stopped", "needs_attention"}


def enabled(connection):
    """A first-article reservation shares the independently disabled goal rollout."""
    return bool(getattr(settings, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", False)
        and getattr(settings, "ARTICLES_SETUP_CONSENT_ENABLED", False)
        and goals.enrolled(connection.organization_id))


def request_key(goal):
    """Keep the charge and worker request stable across source-only goal changes."""
    return "articles-first:" + goals.digest({"goal_id": goal["goal_id"],
        "scope": {key: value for key, value in goal["scope"].items() if key != "configuration_revision"}})


def _binding(goal):
    return {"goal_id": goal["goal_id"],
        "scope": {key: value for key, value in goal["scope"].items() if key != "configuration_revision"},
        "owner_user_id": goal["consent"]["owner_user_id"],
        "answers_digest": goals.digest(goal["consent"]["answers"])}


def _deny():
    raise WebsiteAuthorityError("website_operation_changed",
        "The original first-article payment could not be verified. Refresh this goal before continuing.", status=409)


def _goal(op, *, user):
    goal = goals.mapping(op.receipt).get("goal")
    if (not enabled(op.connection) or op.action != "prepare" or op.state not in ACTIVE
        or op.generation != op.connection.generation or op.connection.state != "connected"
        or not goals.valid_goal(goal, goals.scope_for(op.connection, op.payload.get("company_id")))
        or goal.get("target") != "first_article_live" or goal.get("goal_id") != str(op.pk) or not valid_consent(goal)
        or not getattr(user, "is_active", False)
        or str(user.pk) != goal["consent"]["owner_user_id"]
        or str(user.pk) != str(op.payload.get("requested_by_user_id"))):
        _deny()
    return goal


def _reservation(op, goal):
    receipt = goals.mapping(op.receipt.get("first_article_billing"))
    expected = {"version", "binding", "client_request_id", "payer_user_id", "requester_user_id",
        "cost_points", "spend_ledger_id", "reserved_at", "status", "digest"}
    if (set(receipt) != expected or type(receipt.get("version")) is not int or receipt["version"] != 1
        or receipt.get("binding") != _binding(goal)
        or receipt.get("client_request_id") != request_key(goal)
        or receipt.get("requester_user_id") != goal["consent"]["owner_user_id"]
        or not isinstance(receipt.get("payer_user_id"), str) or not receipt["payer_user_id"].isdecimal()
        or type(receipt.get("cost_points")) is not int or receipt["cost_points"] < 0
        or receipt["cost_points"] != goal["consent"]["answers"]["billing"]["expected_points"]
        or receipt.get("status") not in {"reserved", "consumed", "refunded"}
        or not goals.stamp(receipt.get("reserved_at"))):
        _deny()
    try:
        if receipt["digest"] != goals.digest({key: value for key, value in receipt.items() if key != "digest"}):
            _deny()
    except (ValueError, TypeError, OverflowError):
        _deny()
    return deepcopy(receipt)


def _save(op, receipt):
    receipt = {**receipt, "digest": goals.digest({key: value for key, value in receipt.items() if key != "digest"})}
    op.receipt = {**op.receipt, "first_article_billing": receipt}
    # A credit worker can attach a successor's hold while a prepare claim is
    # waiting. Invalidate that snapshot so it cannot erase the new held receipt.
    # Ordinary generation consumption never mutates this parent receipt.
    op.save(update_fields=["receipt","updated_at"])
    return receipt


def reserve(op, *, user, now, accepted_quote=False):
    """Debit the accepted price inside the goal acceptance transaction, once."""
    goal = goals.mapping(op.receipt).get("goal")
    if goals.mapping(goal).get("target") != "first_article_live" or not enabled(op.connection):
        return None
    goal = _goal(op, user=user)
    if op.receipt.get("first_article_billing") is not None:
        return _reservation(op, goal)
    from .billing import get_content_factory_article_cost_points
    from .company_billing import billing_user_for_organization
    from founder_tools.services import founder_actor_id_for_user
    from roo.permissions import InsufficientBalanceError, IdempotencyConflictError
    from roo.services import PointsService

    cost = goal["consent"]["answers"]["billing"]["expected_points"]
    if not accepted_quote and cost != get_content_factory_article_cost_points(op.connection.organization.domain, user=user):
        raise WebsiteAuthorityError("roo_points_required", "Review the current first-article price before starting.", status=409)
    payer = billing_user_for_organization(op.connection.organization, user)
    key = request_key(goal)
    ledger = None
    if cost:
        try:
            ledger, _ = PointsService.spend(user=payer, delta=cost, source="CONTENT_FACTORY",
                description=f"Reserved first article for {op.connection.organization.domain} (requested by account {user.pk})",
                created_by_slack_id=founder_actor_id_for_user(user),
                idempotency_key=f"content_factory:charge:{key}", reference_type="CONTENT_FACTORY", reference_id=key)
        except InsufficientBalanceError as exc:
            raise WebsiteAuthorityError("roo_points_required", "Add Roo points to reserve the first article.", status=402) from exc
        except IdempotencyConflictError as exc:
            raise WebsiteAuthorityError("website_operation_changed", "The original first-article reservation has changed.", status=409) from exc
    return _save(op, {"version": 1, "binding": _binding(goal), "client_request_id": key,
        "payer_user_id": str(payer.pk), "requester_user_id": str(user.pk), "cost_points": cost,
        "spend_ledger_id": str(ledger.pk) if ledger else None, "reserved_at": now.isoformat(), "status": "reserved"})


def reserve_or_wait(op, *, user, now, predecessor=None):
    """Withdraw old consent immediately while its held payment funds a successor."""
    goal=goals.mapping(op.receipt.get("goal"))
    if not enabled(op.connection) or goal.get("target")!="first_article_live" or op.receipt.get("first_article_billing"):
        return reserve(op,user=user,now=now)
    if op.receipt.get("first_article_billing_wait"):
        return None
    if predecessor is not None:
        previous=goals.mapping(predecessor.receipt.get("first_article_billing"))
        inherited=goals.mapping(predecessor.receipt.get("first_article_billing_wait"))
        from .company_billing import billing_user_for_organization
        payer=billing_user_for_organization(op.connection.organization,user)
        if inherited:
            waiting=deepcopy(inherited)
        elif (predecessor.state=="cancelled" and previous.get("status")=="reserved"
            and previous.get("payer_user_id")==str(payer.pk)
            and type(previous.get("cost_points")) is int
            and previous["cost_points"]>=goal["consent"]["answers"]["billing"]["expected_points"]
            and not any(item["name"]=="first_draft" and item["state"]=="passed"
                for item in goals.mapping(predecessor.receipt.get("goal")).get("milestones",[]))):
            waiting={"predecessor_operation_id":str(predecessor.pk),"client_request_id":previous["client_request_id"],
                "payer_user_id":previous["payer_user_id"]}
        else:
            waiting=None
        if waiting:
            op.receipt={**op.receipt,"first_article_billing_wait":waiting}
            op.save(update_fields=["receipt"])
            return None
    return reserve(op,user=user,now=now)


def reserve_replacement_after_credit(parent, *, now):
    """Credit and re-hold in one transaction, without exposing the funds between goals."""
    from .website_models import WebsiteConnectionOperation
    if not enabled(parent.connection):
        return
    replacement=WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
        connection_id=parent.connection.pk,generation=parent.connection.generation,action="prepare").order_by("-created_at","-pk").first()
    if replacement is None or replacement.state not in ACTIVE or replacement.pk==parent.pk:
        return
    wait=goals.mapping(replacement.receipt.get("first_article_billing_wait"))
    if wait.get("predecessor_operation_id")!=str(parent.pk):
        return
    previous=_reservation(parent,goals.mapping(parent.receipt.get("goal")))
    if (previous["status"]!="refunded" or wait.get("client_request_id")!=previous["client_request_id"]
        or wait.get("payer_user_id")!=previous["payer_user_id"]):
        _deny()
    from django.contrib.auth import get_user_model
    user=get_user_model().objects.filter(pk=replacement.payload.get("requested_by_user_id"),is_active=True).first()
    if user is None:
        return
    # Recheck the same approved payer and quote before spending the just-credited
    # allocation. The initial owner transaction validated this accepted quote.
    from .company_billing import billing_user_for_organization
    from django.db import transaction
    try:
        with transaction.atomic():
            payer=billing_user_for_organization(replacement.connection.organization,user)
            goal=_goal(replacement,user=user)
            if (str(payer.pk)!=previous["payer_user_id"]
                or goal["consent"]["answers"]["billing"]["expected_points"]>previous["cost_points"]):
                _deny()
            reserve(replacement,user=user,now=now,accepted_quote=True)
    except WebsiteAuthorityError:
        replacement.receipt={**replacement.receipt,"first_article_billing_wait_error":"roo_points_required"}
        replacement.save(update_fields=["receipt","updated_at"])


def reservation_ready(op):
    """A replacement waits quietly for its independently durable credit/hold."""
    if not enabled(op.connection) or goals.mapping(op.receipt.get("goal")).get("target")!="first_article_live":
        return True
    if op.receipt.get("first_article_billing"):
        return True
    if op.receipt.get("first_article_billing_wait_error"):
        raise WebsiteAuthorityError("roo_points_required","Review the billing account for this article goal.",status=402)
    if not op.receipt.get("first_article_billing_wait"):
        _deny()
    return False


def _original_spend(receipt, *, allow_refunded=False):
    from roo.models import Ledger
    if receipt["cost_points"] == 0:
        if receipt["spend_ledger_id"] is not None:
            _deny()
        return None
    identifier = receipt.get("spend_ledger_id")
    if not isinstance(identifier, str) or not identifier.isdecimal() or len(identifier) > 19:
        _deny()
    key = receipt["client_request_id"]
    # Ledger.user is nullable for legacy rows. PostgreSQL rejects an unscoped
    # FOR UPDATE across that outer join, so lock only the ledger row itself.
    # The payer relation is read for its active-account check, not locked.
    ledger = Ledger.objects.select_for_update(of=("self",)).select_related("user").filter(pk=identifier).first()
    if (not ledger or str(ledger.user_id) != receipt["payer_user_id"] or ledger.kind != "SPEND"
        or ledger.source != "CONTENT_FACTORY" or ledger.reference_type != "CONTENT_FACTORY"
        or ledger.reference_id != key or ledger.idempotency_key != f"content_factory:charge:{key}"
        or ledger.delta_microroo != -receipt["cost_points"] * 1_000_000
        or not allow_refunded and Ledger.objects.filter(idempotency_key=f"content_factory:refund:{key}").exists()):
        _deny()
    return ledger


def for_generation(operation_id, *, context, user):
    """Reload server-held authority; an HTTP body cannot select a free/held charge."""
    from .website_models import WebsiteConnectionOperation
    op = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
        pk=operation_id, action="prepare", connection__organization_id=context.organization.pk,
        payload__company_id=str(context.company.pk)).first()
    if not op:
        _deny()
    goal = _goal(op, user=user)
    latest = op.connection.operations.filter(generation=op.generation, action="prepare").order_by("-created_at", "-pk").first()
    if latest is None or latest.pk != op.pk:
        _deny()
    receipt = _reservation(op, goal)
    if receipt["status"] == "refunded":
        _deny()
    ledger = _original_spend(receipt)
    from django.contrib.auth import get_user_model
    payer = ledger.user if ledger else get_user_model().objects.filter(pk=receipt["payer_user_id"], is_active=True).first()
    if payer is None or not payer.is_active:
        _deny()
    return op, receipt, payer, ledger


def consume(operation_id, *, context, user, payload):
    """Reuse the exact original payer/amount without consulting today's price."""
    op, receipt, payer, ledger = for_generation(operation_id, context=context, user=user)
    expected = {"website_connection_id":str(op.connection.pk), "connection_generation":op.connection.generation,
        "repository_id":op.connection.repository_id, "branch":op.connection.branch, "app_root":op.connection.app_root}
    if (payload.get("client_request_id") != receipt["client_request_id"]
        or payload.get("delivery_mode") not in {"publish_code", "review_draft"}
        or any(type(payload.get(key)) is not type(value) or payload.get(key) != value for key, value in expected.items())):
        _deny()
    payload.update(first_article_goal_operation_id=str(op.pk), first_article_goal_id=op.receipt["goal"]["goal_id"],
        roo_points_requested_by_user_id=user.pk, roo_points_billing_user_id=payer.pk)
    # Keep the held receipt immutable while a leased prepare runs. The durable
    # generation outbox records consumption; a stale parent snapshot must never
    # overwrite a newer payment status after remote dispatch.
    return payer, ledger, receipt["cost_points"]


def generation_key(operation_id, *, context, user):
    """Read the held request identity before ordinary generation admission checks."""
    from django.db import transaction
    with transaction.atomic():
        _, receipt, _, _ = for_generation(operation_id, context=context, user=user)
        return receipt["client_request_id"]


def defer_child_refund(article_request, *, resolved_domain):
    """Keep an active goal's original spend available for its repair attempts."""
    identifier = article_request.get("first_article_goal_operation_id")
    if not identifier:
        return False
    from .website_models import WebsiteConnectionOperation
    try:
        op = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(pk=identifier, action="prepare").first()
    except (ValueError, TypeError, ValidationError):
        return False
    if op is None or op.state not in ACTIVE or op.connection.state != "connected":
        return False
    goal = goals.mapping(op.receipt.get("goal"))
    if (not goals.valid_goal(goal, goals.scope_for(op.connection, op.payload.get("company_id")))
        or goal.get("target") != "first_article_live" or not valid_consent(goal)
        or goal.get("goal_id") != str(op.pk)
        or op.connection.organization.domain.casefold() != str(resolved_domain).casefold()):
        return False
    try:
        receipt = _reservation(op, goal)
    except WebsiteAuthorityError:
        return False
    return (receipt["status"] != "refunded" and receipt["client_request_id"] == article_request.get("client_request_id")
        and str(article_request.get("roo_points_ledger_id") or "") == str(receipt["spend_ledger_id"] or "")
        and str(article_request.get("roo_points_billing_user_id")) == receipt["payer_user_id"])


def release(op, *, reason):
    """Credit a terminal goal's exact original spend through the existing refund path.

    The caller holds the goal lock, after its terminal state wins its lease CAS.
    A rollout change or disconnected site must not strand an existing payment.
    """
    goal = goals.mapping(op.receipt.get("goal"))
    if not op.receipt.get("first_article_billing"):
        return None
    if (op.state not in REFUND_TERMINAL
        or not goals.valid_goal(goal, goal.get("scope")) or not valid_consent(goal)
        or goal.get("goal_id") != str(op.pk)):
        _deny()
    receipt = _reservation(op, goal)
    if receipt["status"] == "refunded":
        return receipt
    if op.state in {"cancelled", "denied", "deleted"} and any(
        item["name"] == "first_draft" and item["state"] == "passed"
        and goals.valid_proof("first_draft", item.get("proof"), scope=goal["scope"],
            source_sha=goals.mapping(item.get("proof")).get("source_sha"),
            now=goals.stamp(goals.mapping(item.get("proof")).get("at"))) for item in goal["milestones"]):
        return receipt
    # A crash after credit but before saving this receipt reuses the refund key.
    # Validate the spend without rejecting that already committed refund here.
    ledger = _original_spend(receipt, allow_refunded=True)
    if ledger:
        from founder_tools.services import founder_actor_id_for_user
        from integrations.services.article_generation import _refund_content_factory_request
        _refund_content_factory_request(user=ledger.user,
            slack_user_id=founder_actor_id_for_user(ledger.user),
            article_request={"client_request_id":receipt["client_request_id"],
                "roo_points_ledger_id":receipt["spend_ledger_id"], "roo_points_cost":receipt["cost_points"]},
            resolved_domain=op.connection.organization.domain, reason=reason)
    return _save(op, {**receipt, "status":"refunded"})


def cancel_held_goals(connection):
    """Preserve and settle held receipts before lifecycle generation invalidation."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection,
        generation=connection.generation, action="prepare", receipt__has_key="first_article_billing").exclude(state="completed")
    for op in rows:
        op.state, op.next_attempt_at = "cancelled", None
        op.receipt = {**op.receipt, "status":"authority_revoked", "repository_modified":False, "user_action":None}
        op.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
        from .website_first_article_refunds import reserve_refund
        reserve_refund(op)
