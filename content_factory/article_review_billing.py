"""Idempotent editor image charges using the existing Roo ledger."""
import hashlib

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response

from .billing import is_free_content_factory_domain
from .website_contract import evidence_digest


def image_regeneration_cost(domain):
    """Return the advertised integer price, including the existing free domain."""
    if is_free_content_factory_domain(domain):
        return 0
    value = getattr(settings, "CONTENT_FACTORY_IMAGE_REGENERATION_COST_POINTS", None)
    return value if type(value) is int and value >= 0 else None


def reserve_image_charge(*, user, context, run, payload):
    """Bind one exact operation to its payer, company, saved run and spend."""
    from roo.models import Ledger
    from roo.permissions import InsufficientBalanceError
    from roo.services import PointsService
    cost = image_regeneration_cost(context.organization.domain)
    if cost is None:
        return None, Response({"code": "image_regeneration_quote_unavailable", "detail": "Image regeneration pricing is unavailable. Try again after pricing is configured."}, status=409)
    quoted = payload.get("expectedCostPoints")
    if type(quoted) is not int or quoted != cost:
        return None, Response({"code": "roo_points_quote_changed", "detail": "Review the updated image price.", "costPoints": cost}, status=409)
    identifier = payload.get("operationId")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200:
        return None, Response({"code": "image_operation_id_required", "detail": "Reload the editor and try again."}, status=400)
    identity = hashlib.sha256(f"{context.organization.id}:{run.run_id}:{identifier}".encode()).hexdigest()
    key = f"content_factory:image:{identity}"
    digest = evidence_digest({k: v for k, v in payload.items() if k != "expectedCostPoints"})
    if Ledger.objects.filter(user=user, idempotency_key=f"{key}:refund").exists():
        return None, Response({"code": "roo_points_billing_refunded", "detail": "That image request was refunded. Start a new image request."}, status=409)
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        saved = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = dict(saved.result or {})
        receipts = dict(result.get("article_image_billing") or {})
        previous = next((item for item in receipts.values() if isinstance(item, dict)
            and item.get("operationId") == identifier), receipts.get(identity))
        if previous and previous.get("userId") != user.pk:
            return None, Response({"code": "image_operation_owner_conflict", "detail": "That image request belongs to another member. Start a new image request."}, status=409)
        if previous:
            identity, key = previous["identity"], previous["key"]
        if previous and previous.get("status") == "refunded":
            return None, Response({"code": "roo_points_billing_refunded", "detail": "That image request was refunded. Start a new image request."}, status=409)
        if previous and previous.get("digest") != digest:
            return None, Response({"code": "image_operation_conflict", "detail": "That image request changed. Start a new request."}, status=409)
        if previous and previous.get("costPoints") != cost:
            return None, Response({"code": "roo_points_quote_changed", "detail": "Review the new price and start a new image request.", "costPoints": cost}, status=409)
        if cost and not previous:
            try:
                ledger, _ = PointsService.spend(user=user, delta=cost, source="CONTENT_FACTORY",
                    description=f"Regenerate article image for {context.organization.domain}",
                    created_by_slack_id="", idempotency_key=f"{key}:charge", reference_type="CONTENT_FACTORY_IMAGE", reference_id=identity)
            except InsufficientBalanceError:
                return None, Response({"code": "roo_points_insufficient", "detail": "Add Roo Points to regenerate this image.", "costPoints": cost}, status=402)
        else:
            ledger = None
        receipt = previous or {"digest": digest, "costPoints": cost, "ledgerId": getattr(ledger, "id", None),
            "status": "reserved", "key": key, "identity": identity, "operationId": identifier,
            "fieldId": payload.get("fieldId"), "userId": user.pk, "recordedAt": timezone.now().isoformat()}
        receipts[identity] = receipt
        saved.result = {**result, "article_image_billing": receipts}
        saved.save(update_fields=["result", "updated_at"])
        run.result = saved.result
    return receipt, None


def settle_image_charge(*, user, run, receipt, rejected=False, pending=False):
    """Refund only definite rejections; ambiguous provider acceptance holds debit."""
    from roo.services import PointsService
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        saved = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        receipts = dict((saved.result or {}).get("article_image_billing") or {})
        previous = receipts.get(receipt["identity"]) or {}
        if previous.get("status") == "refunded" or (previous.get("status") == "accepted" and pending):
            return
        receipt = previous or receipt
        if rejected and receipt["costPoints"]:
            if user.pk != receipt.get("userId"):
                from django.contrib.auth import get_user_model
                user = get_user_model().objects.filter(pk=receipt.get("userId")).first()
                if user is None:
                    return
            PointsService.refund(user=user, delta=receipt["costPoints"], source="CONTENT_FACTORY",
                description="Refund for failed article image regeneration", created_by_slack_id="",
                idempotency_key=f"{receipt['key']}:refund", original_spend_key=f"{receipt['key']}:charge",
                reference_type="CONTENT_FACTORY_IMAGE", reference_id=receipt["identity"])
        receipts[receipt["identity"]] = {**receipt, "status": "refunded" if rejected else "pending" if pending else "accepted"}
        saved.result = {**(saved.result or {}), "article_image_billing": receipts}
        saved.save(update_fields=["result", "updated_at"])
        run.result = saved.result


def reconcile_image_charges(*, user, run, snapshot):
    """Refund accepted asynchronous requests only after their exact job failed."""
    candidates = snapshot.get("candidates") if isinstance(snapshot, dict) else None
    if not isinstance(candidates, dict):
        return
    for receipt in list(((run.result or {}).get("article_image_billing") or {}).values()):
        if not isinstance(receipt, dict) or receipt.get("status") == "refunded":
            continue
        candidate = candidates.get(receipt.get("operationId"))
        if (isinstance(candidate, dict) and candidate.get("status") == "failed"
                and candidate.get("fieldId") == receipt.get("fieldId")):
            payer = user
            if receipt.get("userId") != user.pk:
                from django.contrib.auth import get_user_model
                payer = get_user_model().objects.filter(pk=receipt.get("userId")).first()
            if payer is not None:
                settle_image_charge(user=payer, run=run, receipt=receipt, rejected=True)
