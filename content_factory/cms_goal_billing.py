"""Native first-article holds reuse original financial records, never Git scope."""

from copy import deepcopy

from django.db import transaction

from . import cms_goal_authority as authority
from . import website_first_article_billing as records
from .channel_artifact_contract import canonical_bytes
from .website_contract import WebsiteAuthorityError


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native article payment could not be verified.")


def _financial_context(value):
    # These common helpers bind opaque scope/answer data and original ledger
    # fields. No legacy repository goal validation or permission is reused.
    return {**value.goal, "target": "first_article_live", "consent": value.consent}


def request_key(value):
    """Keep the original native charge/outbox identity across worker retries."""
    return records.request_key(_financial_context(value))


def _reservation(operation, value):
    goal = _financial_context(value)
    receipt = records._reservation(operation, goal)
    if (canonical_bytes(receipt["binding"]) != canonical_bytes(records._binding(goal)) or
            len(receipt["payer_user_id"]) > 19 or not 0 < int(receipt["payer_user_id"]) < 2**63):
        raise _deny()
    return receipt


def _mark_financial_records(connection):
    if connection.capabilities.get("_cms_financial_records") is not True:
        connection.capabilities = {**connection.capabilities, "_cms_financial_records": True}
        connection.save(update_fields=["capabilities", "updated_at"])


def reserve(operation, *, user, now):
    """Hold the explicitly accepted current price in the owner's start transaction."""
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    value = authority.current(operation, user=user)
    if operation.receipt.get("first_article_billing") is not None:
        receipt = _reservation(operation, value)
        _mark_financial_records(value.connection)
        return receipt
    if operation.state != "pending" or operation.payload["source_run_id"] is not None:
        raise _deny()
    from .billing import get_content_factory_article_cost_points
    from .company_billing import billing_user_for_organization
    from founder_tools.services import founder_actor_id_for_user
    from roo.permissions import InsufficientBalanceError, IdempotencyConflictError
    from roo.services import PointsService
    cost = value.consent["answers"]["billing"]["expected_points"]
    if cost != get_content_factory_article_cost_points(value.context.organization.domain):
        raise WebsiteAuthorityError("roo_points_required", "Review the current first-article price before starting.", status=409)
    payer = billing_user_for_organization(value.context.organization, user)
    key, ledger = request_key(value), None
    if cost:
        try:
            ledger, _created = PointsService.spend(user=payer, delta=cost, source="CONTENT_FACTORY",
                description=f"Reserved first native CMS article for {value.context.organization.domain} (requested by account {user.pk})",
                created_by_slack_id=founder_actor_id_for_user(user), idempotency_key="content_factory:charge:" + key,
                reference_type="CONTENT_FACTORY", reference_id=key)
        except InsufficientBalanceError:
            raise WebsiteAuthorityError("roo_points_required", "Add Roo points to reserve the first article.", status=402) from None
        except IdempotencyConflictError:
            raise _deny() from None
    receipt = records._save(operation, {"version": 1, "binding": records._binding(_financial_context(value)),
        "client_request_id": key, "payer_user_id": str(payer.pk), "requester_user_id": str(user.pk),
        "cost_points": cost, "spend_ledger_id": str(ledger.pk) if ledger else None,
        "reserved_at": now.isoformat(), "status": "reserved"})
    _mark_financial_records(value.connection)
    return receipt


def for_generation(dispatch, *, context, user):
    """Require the server's current native lease and original active payer/spend."""
    if not isinstance(dispatch, authority.CmsGoalDispatch) or not transaction.get_connection().in_atomic_block:
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    from django.contrib.auth import get_user_model
    from roo.models import Ledger
    Organization.objects.select_for_update().get(pk=context.organization.pk)
    operation = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
        pk=dispatch.operation_id, action=authority.ACTION, connection__organization_id=context.organization.pk,
        payload__company_id=str(context.company.pk)).first()
    if operation is None:
        raise _deny()
    value = authority.current(operation, user=user, lease_id=dispatch.lease_id)
    latest = operation.connection.operations.filter(action=authority.ACTION, generation=operation.generation).order_by("-created_at", "-pk").first()
    if latest is None or latest.pk != operation.pk:
        raise _deny()
    from .cms_goal_budget import exhaustion
    from django.utils import timezone
    if exhaustion(operation, now=timezone.now(), action="first-article-generation") is not None:
        raise _deny()
    receipt = _reservation(operation, value)
    if receipt["status"] == "refunded" or Ledger.objects.filter(idempotency_key="content_factory:refund:" + receipt["client_request_id"]).exists():
        raise _deny()
    ledger = records._original_spend(receipt)
    payer = ledger.user if ledger else get_user_model().objects.filter(pk=receipt["payer_user_id"], is_active=True).first()
    if payer is None or payer.is_active is not True:
        raise _deny()
    return operation, value, receipt, payer, ledger


def generation_key(dispatch, *, context, user):
    """Read the original held identity before ordinary generation admission checks."""
    with transaction.atomic():
        _operation, _value, receipt, _payer, _ledger = for_generation(dispatch, context=context, user=user)
        return receipt["client_request_id"]


def consume(dispatch, *, context, user, payload):
    """Use only the exact original hold inside the canonical artifact/outbox transaction."""
    operation, value, receipt, payer, ledger = for_generation(dispatch, context=context, user=user)
    from .channel_artifacts import authorize_payload
    admitted = authorize_payload(payload, require_outbox=False)
    if (admitted.scope.binding != value.scope["binding"] or
            payload.get("client_request_id") != receipt["client_request_id"] or
            payload.get("cms_goal_operation_id") != str(operation.pk) or payload.get("cms_goal_id") != value.goal["goal_id"] or
            payload.get("delivery_mode") != "content_only" or payload.get("delivery_mode_confirmed") is not True or payload.get("github_repo")):
        raise _deny()
    payload.update(roo_points_requested_by_user_id=user.pk, roo_points_billing_user_id=payer.pk,
        roo_points_ledger_id=str(ledger.pk) if ledger else None, roo_points_cost=receipt["cost_points"])
    return payer, ledger, receipt["cost_points"]


def historical_reservation(operation):
    """Resolve private original financial identity after revocation, without granting effects."""
    value = authority.historical(operation)
    return deepcopy(_reservation(operation, value))


def retention_subject(operation):
    """Return a minimized private reference to useful original generated work."""
    from . import cms_goal_contract as goals
    value = authority.historical(operation)
    for row in value.goal["milestones"]:
        if row["name"] == "first_draft" and row["state"] == "passed" and goals.valid_proof("first_draft", row["proof"],
                expected_scope=value.scope, original_source=value.goal["source"], now=goals._time(row["proof"]["at"])):
            return {"basis": "goal_first_draft", "evidence_digest": row["proof"]["digest"]}
    history = next((row for row in reversed(value.goal["proof_history"]) if row["name"] == "first_draft"), None)
    if history is not None:
        return {"basis": "goal_draft_history", "evidence_digest": history["proof_digest"]}
    # The media/publisher scheduler may finish before the parent ledger wakes.
    # Read the backend's original private accepted handoff, never a worker flag.
    from .website_models import WebsiteConnectionOperation
    from .cms_artifact_review import validate_review_surface, ACTION as REVIEW_ACTION
    from .channel_artifact_contract import digest
    receipt = _reservation(operation, value)
    review = WebsiteConnectionOperation.objects.filter(action=REVIEW_ACTION, state="completed",
        connection_id=operation.connection_id, generation=operation.generation,
        payload__handoff__envelope__billing_request_id=receipt["client_request_id"]).order_by("-created_at", "-pk").first()
    if review is None:
        return None
    try:
        payload = review.payload
        handoff = validate_review_surface(payload["handoff"])
        envelope = handoff["envelope"]
        expected = {"status": "artifact_received", "channel": "cms", "sourceRunId": handoff["run_id"], "artifactDigest": handoff["digest"],
            "reviewRevision": handoff["review_revision"], "provider_modified": False, "publishing_verified": False, "approval_recorded": False}
        if (payload["binding"] != value.scope["binding"] or payload["source_run_id"] != handoff["run_id"] or
                type(payload["source_generation"]) is not int or payload["source_generation"] != handoff["run_generation"] or
                payload["credential_operation_id"] != operation.payload["credential_operation_id"] or
                envelope["scope_sha256"] != digest(value.scope["binding"]) or envelope["billing_request_id"] != receipt["client_request_id"] or
                canonical_bytes(review.receipt) != canonical_bytes(expected)):
            raise _deny()
        return {"basis": "private_native_handoff", "evidence_digest": handoff["digest"]}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def retains_after_draft(operation):
    """Keep the accepted generation charge after useful original draft evidence."""
    return retention_subject(operation) is not None


def _validate_credit(credited, receipt):
    if (credited is None or str(getattr(credited, "user_id", None)) != receipt["payer_user_id"] or
            type(getattr(credited, "delta_microroo", None)) is not int or
            getattr(credited, "delta_microroo", None) != receipt["cost_points"] * 1_000_000 or
            getattr(credited, "idempotency_key", None) != "content_factory:refund:" + receipt["client_request_id"]):
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original native article credit is still pending.", retryable=True)


def existing_credit(operation):
    """Observe the real original credit before accepting a stale parent stamp."""
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    from roo.models import Ledger
    value = authority.historical(operation)
    receipt = _reservation(operation, value)
    records._original_spend(receipt, allow_refunded=True)
    rows = Ledger.objects.filter(idempotency_key="content_factory:refund:" + receipt["client_request_id"])
    if not rows.exists():
        return None
    _validate_credit(rows.first(), receipt)
    return records._save(operation, {**receipt, "status": "refunded"})


def defer_child_refund(article_request, *, resolved_domain):
    """Keep active native repair funds using only the original backend outbox.

    A worker dropping/changing goal fields cannot erase the original hold.
    The input selects a request key; the server row supplies all goal authority.
    """
    from .dispatch_models import ContentFactoryDispatchOutbox
    from .website_models import WebsiteConnectionOperation
    from .billing import normalize_content_factory_domain
    from .channel_artifact_contract import validate_envelope, ArtifactContractError, digest
    try:
        key = article_request.get("client_request_id")
        if not isinstance(key, str) or not 0 < len(key) <= 255:
            return False
        outbox = ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).first()
        from . import cms_goal_settlement as settlements
        if outbox and isinstance(outbox.payload, dict) and settlements.REFERENCE in outbox.payload:
            stamp = settlements.validate(outbox)
            return (stamp["decision"] == "retained" and normalize_content_factory_domain(resolved_domain) ==
                normalize_content_factory_domain(outbox.payload["request"]["domain"]))
        original = outbox.payload.get("request") if outbox and isinstance(outbox.payload, dict) else None
        identifier = original.get("cms_goal_operation_id") if isinstance(original, dict) else None
        if not isinstance(identifier, str):
            return False
        operation = WebsiteConnectionOperation.objects.filter(pk=identifier, action=authority.ACTION).first()
        if operation is None:
            return False
        value = authority.historical(operation)
        receipt = _reservation(operation, value)
        envelope = validate_envelope(original.get("publishing_channel_artifact"))
        binding = value.scope["binding"]
        if (receipt["status"] == "refunded" or key != receipt["client_request_id"] or original.get("client_request_id") != key or
                original.get("cms_goal_id") != value.goal["goal_id"] or str(outbox.organization_id) != binding["organization_id"] or
                normalize_content_factory_domain(resolved_domain) != normalize_content_factory_domain(operation.connection.organization.domain) or
                original.get("delivery_mode") != "content_only" or original.get("delivery_mode_confirmed") is not True or original.get("github_repo") or
                envelope["channel"] != "cms" or envelope["billing_request_id"] != key or
                envelope["company_id"] != binding["company_id"] or envelope["organization_id"] != binding["organization_id"] or
                envelope["owner_user_id"] != binding["owner_user_id"] or envelope["scope_sha256"] != digest(binding) or
                envelope["connection_id"] != binding["connection_id"] or envelope["connection_generation"] != binding["generation"]):
            return False
        return operation.state in authority.ACTIVE or retains_after_draft(operation)
    except (WebsiteAuthorityError, ArtifactContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return False


def release(operation, *, reason):
    """Settle a terminal native hold once, even after revocation or rollout-off."""
    if not transaction.get_connection().in_atomic_block or operation.state not in records.REFUND_TERMINAL:
        raise _deny()
    value = authority.historical(operation)
    receipt = _reservation(operation, value)
    if receipt["status"] == "refunded":
        return receipt
    observed = existing_credit(operation)
    if observed is not None:
        return observed
    ledger = records._original_spend(receipt, allow_refunded=True)
    if retains_after_draft(operation):
        return receipt
    if ledger:
        from founder_tools.services import founder_actor_id_for_user
        from integrations.services.article_generation import _refund_content_factory_request
        credited = _refund_content_factory_request(user=ledger.user, slack_user_id=founder_actor_id_for_user(ledger.user),
            article_request={"client_request_id": receipt["client_request_id"], "cms_goal_operation_id": str(operation.pk),
                "roo_points_ledger_id": receipt["spend_ledger_id"], "roo_points_cost": receipt["cost_points"]},
            resolved_domain=operation.connection.organization.domain, reason=reason)
        _validate_credit(credited, receipt)
    return records._save(operation, {**receipt, "status": "refunded"})
