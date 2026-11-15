"""Private treasury operations, serialized on the organisation's ledger."""
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from integrations.models import StudioPaymentReview, StudioTreasuryLedger
from .studio_payment_rules import ReviewError, digest, review, claim_fingerprint
from .studio_treasury_sources import StudioSources


def scope_for(workspace, actor, *, treasury=True):
    scope = StudioTreasuryLedger.objects.get(workspace_id=workspace)
    allowed = scope.configuration.get("treasurers", [])
    if not treasury:
        allowed = list(set(allowed) | set(scope.configuration.get("members", [])))
    if actor not in allowed:
        raise PermissionError("studio_request_not_authorized")
    return scope


def result_for(item):
    return {"id": str(item.id), "status": item.status, "expires_at": item.expires_at.isoformat(),
            "invoices": item.result["invoices"], "ready_cents": item.result["ready_cents"],
            "exceptions": item.result.get("exceptions", []), "handoffs": item.result.get("handoffs", [])}


def evaluate(scope, snapshot):
    checked = review(snapshot["claims"], snapshot["work"], snapshot["funding"], scope.reservations,
                     opening_verified=bool(scope.opening_verified_at and scope.opening_evidence))
    uncertain = {e["builder"] for e in snapshot["exceptions"] if e.get("builder")}
    for row in checked["invoices"]:
        if row["status"] == "ready" and row["invoice"].split(":", 1)[0] in uncertain:
            row["status"] = "needs_review"
            row["errors"].append({"code": "invoice_revision_or_extraction_needs_review"})
    ready = {row["invoice"] for row in checked["invoices"] if row["status"] == "ready"}
    checked["allocations"] = [a for a in checked["allocations"] if a["invoice"] in ready]
    checked["ready_cents"] = sum(r.get("cents", 0) for r in checked["invoices"] if r["invoice"] in ready)
    checked["exceptions"] = snapshot["exceptions"]
    return checked


def perform(workspace, actor, operation, *, request_key, selector="recent", review_id=None,
            invoice=None, receipt_id=None, source_factory=StudioSources):
    scope = scope_for(workspace, actor)
    if operation == "preview":
        with transaction.atomic():
            scope = StudioTreasuryLedger.objects.select_for_update().get(pk=scope.pk)
            existing = StudioPaymentReview.objects.filter(ledger=scope, request_key=request_key).first()
            if existing:
                return result_for(existing)
            snapshot = source_factory(scope).snapshot(selector)
            checked = evaluate(scope, snapshot)
            item = StudioPaymentReview.objects.create(ledger=scope, requester=actor, request_key=request_key,
                claims={"selector": selector, "invoices": snapshot["claims"]}, result=checked,
                source_hash=digest(snapshot), expires_at=timezone.now() + timedelta(minutes=30))
            return result_for(item)
    with transaction.atomic():
        scope = StudioTreasuryLedger.objects.select_for_update().get(pk=scope.pk)
        item = StudioPaymentReview.objects.get(pk=UUID(review_id), ledger=scope, requester=actor)
        if operation == "show":
            return result_for(item)
        if operation == "authorize":
            if item.status in {"reserved", "paid", "partial"}:
                return result_for(item)
            if item.expires_at <= timezone.now():
                raise ReviewError("review_expired")
            snapshot = source_factory(scope).snapshot(item.claims["selector"])
            if digest(snapshot) != item.source_hash:
                raise ReviewError("sources_changed_request_new_review")
            checked = evaluate(scope, snapshot)
            previous_ready = {r["invoice"] for r in item.result["invoices"] if r["status"] == "ready"}
            current_ready = {r["invoice"] for r in checked["invoices"] if r["status"] == "ready"}
            if not previous_ready or current_ready != previous_ready:
                raise ReviewError("coverage_changed_request_new_review")
            handoffs = []
            for row in checked["invoices"]:
                if row["status"] != "ready":
                    continue
                key = digest({"review": str(item.id), "invoice": row["invoice"]})
                reference = "ROO-" + key[:16].upper()
                scope.reservations[key] = {"status": "reserved", "invoices": [row["invoice"]],
                    "claim_hash": claim_fingerprint(next(claim for claim in snapshot["claims"]
                        if claim["builder"] + ":" + claim["number"].strip().casefold() == row["invoice"])),
                    "allocations": [a for a in checked["allocations"] if a["invoice"] == row["invoice"]],
                    "reference": reference, "review": str(item.id), "actor": actor}
                handoffs.append({"invoice": row["invoice"], "number": row["number"], "reference": reference,
                                 "cents": row["cents"], "currency": row["currency"], "status": "reserved"})
            item.result = {**checked, "exceptions": snapshot["exceptions"], "handoffs": handoffs}
            item.status = "reserved"
        elif operation in {"receipt", "unknown"}:
            if item.status not in {"reserved", "partial", "paid"}:
                raise ReviewError("payment_not_reserved")
            handoff = next((h for h in item.result["handoffs"] if h["invoice"] == invoice or h["number"] == invoice), None)
            if handoff is None:
                raise ReviewError("invoice_not_in_handoff")
            key = digest({"review": str(item.id), "invoice": handoff["invoice"]})
            reservation = scope.reservations[key]
            if reservation["status"] == "paid":
                if operation == "receipt" and reservation["receipt"]["id"] != str(UUID(receipt_id)):
                    raise ReviewError("receipt_replay_mismatch")
                return result_for(item)
            if operation == "unknown":
                reservation["status"] = "unknown"
                handoff["status"] = "unknown"
            else:
                row = next(r for r in item.result["invoices"] if r["invoice"] == handoff["invoice"])
                receipt = source_factory(scope).bank_receipt(receipt_id, handoff["reference"], row)
                if any(r.get("receipt", {}).get("id") == receipt["id"] for r in scope.reservations.values()):
                    raise ReviewError("bank_receipt_already_used")
                reservation.update(status="paid", receipt=receipt)
                handoff["status"] = "paid"
                item.bank_receipt[handoff["invoice"]] = receipt
            item.status = "paid" if all(h["status"] == "paid" for h in item.result["handoffs"]) else "partial"
        else:
            raise ReviewError("unsupported_operation")
        scope.save(update_fields=["reservations", "updated_at"])
        item.save(update_fields=["result", "status", "bank_receipt", "updated_at"])
        return result_for(item)
