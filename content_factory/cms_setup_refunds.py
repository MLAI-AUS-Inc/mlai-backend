"""Durable unused setup credits, independent of provider keys and rollout flags."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import cms_setup_authority as authority
from . import cms_setup_billing as billing
from .cms_setup_contract import validate_intent
from .website_contract import WebsiteAuthorityError

ACTION = "cms-setup-refund"
FIELDS = {"schema_version", "setup_operation_id", "intent_digest", "purge_after_reconciliation"}


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native setup credit could not be verified.")


def _validate(row, parent):
    intent = authority.historical(parent)
    payload = row.payload
    if (row.action != ACTION or parent.state not in billing.records.REFUND_TERMINAL or not isinstance(payload, dict) or
            set(payload) != FIELDS or type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
            type(payload["purge_after_reconciliation"]) is not bool or payload["setup_operation_id"] != str(parent.pk) or
            payload["intent_digest"] != intent["digest"] or row.connection_id != parent.connection_id or row.generation != parent.generation or
            row.idempotency_key != f"{parent.connection_id}:cms-setup-refund:{parent.pk}"):
        raise _deny()
    billing.historical_reservation(parent)
    return intent


def reserve_refund(parent, *, now=None):
    """Queue original unused settlement in the same transaction as cancellation."""
    if parent.state not in billing.records.REFUND_TERMINAL or not parent.receipt.get("first_article_billing"):
        return None
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    intent = authority.historical(parent)
    billing.historical_reservation(parent)
    from .website_models import WebsiteConnectionOperation
    row, _created = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{parent.connection_id}:cms-setup-refund:{parent.pk}", defaults={
            "connection": parent.connection, "generation": parent.generation, "action": ACTION,
            "state": "pending", "next_attempt_at": now or timezone.now(),
            "payload": {"schema_version": 1, "setup_operation_id": str(parent.pk), "intent_digest": intent["digest"],
                "purge_after_reconciliation": False}, "receipt": {"status": "credit_pending"}})
    _validate(row, parent)
    return row


def erase_intent(parent):
    """Erase original planning data only after its held-money proof is independent."""
    from .website_models import WebsiteConnectionOperation
    child = WebsiteConnectionOperation.objects.filter(idempotency_key="cms-setup-intent:" + str(parent.pk),
        action=authority.INTENT_ACTION, connection_id=parent.connection_id, generation=parent.generation, state="completed").first()
    if child is None:
        return
    intent = validate_intent(child.payload, expected_binding=parent.payload["binding"])
    if intent["operation_id"] != str(parent.pk):
        raise _deny()
    child.payload, child.receipt, child.state = {}, {"status": "erased"}, "deleted"
    child.save(update_fields=["payload", "receipt", "state", "updated_at"])


def _erase(parent):
    erase_intent(parent)
    parent.payload, parent.receipt, parent.state, parent.next_attempt_at = {}, {"status": "financial_records_settled"}, "deleted", None
    parent.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])


def advance_refund(identifier, *, now=None):
    """Credit unused money under database locks; never call a provider or CF worker."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None:
        return None
    now, before = now or timezone.now(), None
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if row is None or row.state not in {"pending", "running"} or row.next_attempt_at and row.next_attempt_at > now:
                return None
            before = row.attempts
            parent = WebsiteConnectionOperation.objects.select_for_update().filter(pk=row.payload.get("setup_operation_id"),
                action=authority.ACTION, connection_id=row.connection_id, generation=row.generation).first()
            if parent is None:
                raise _deny()
            _validate(row, parent)
            receipt = billing.release(parent, reason="Native setup ended before article generation.")
            if receipt["status"] != "refunded":
                raise _deny()
            row.state, row.next_attempt_at, row.attempts = "completed", None, before + 1
            row.receipt = {"status": "credited", "completed_at": now.isoformat()}
            row.save(update_fields=["state", "next_attempt_at", "attempts", "receipt", "updated_at"])
            if row.payload["purge_after_reconciliation"]:
                _erase(parent)
            return "completed"
    except Exception as exc:
        if before is None:
            raise
        permanent = not exc.retryable if isinstance(exc, WebsiteAuthorityError) else isinstance(exc, (ValueError, TypeError))
        state = "needs_attention" if permanent else "pending"
        # A transaction whose acknowledgement was lost already incremented its
        # attempt. The old snapshot cannot replace that committed completion.
        changed = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, attempts=before,
            state__in=["pending", "running"]).update(state=state, attempts=before + 1,
                next_attempt_at=None if permanent else now + timedelta(minutes=5), updated_at=now,
                receipt={"status": "credit_needs_review" if permanent else "credit_pending"})
        if not changed:
            observed = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
            return observed.state if observed is not None else None
        return state


def cancel_held_setups(connection, *, all_generations=False):
    """Fence setup leases before credentials change and retain their original money."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection, action=authority.ACTION,
        receipt__has_key="first_article_billing")
    if not all_generations:
        rows = rows.filter(generation=connection.generation)
    for parent in rows:
        authority.historical(parent)
        parent.state, parent.next_attempt_at = "cancelled", None
        parent.payload = {key: value for key, value in parent.payload.items() if key != "lease_id"}
        parent.receipt = {**parent.receipt, "status": "authority_revoked"}
        parent.receipt.pop("user_action", None)
        parent.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
        reserve_refund(parent)


def retain_for_erasure(website):
    """Keep the exact original unused-hold proof until its real credit is recorded."""
    for parent in website.operations.filter(action=authority.ACTION, receipt__has_key="first_article_billing"):
        row = reserve_refund(parent)
        if row is None:
            raise _deny()
        if row.state == "completed":
            _validate(row, parent)
            _erase(parent)
        else:
            row.payload = {**row.payload, "purge_after_reconciliation": True}
            row.save(update_fields=["payload", "updated_at"])
            # Drop execution diagnostics, but keep the original private intent
            # and receipt needed to verify the future credit after revocation.
            parent.receipt = {"first_article_billing": parent.receipt["first_article_billing"]}
            parent.save(update_fields=["receipt", "updated_at"])
    return website.operations.filter(action=ACTION).exclude(state="completed").exists()
