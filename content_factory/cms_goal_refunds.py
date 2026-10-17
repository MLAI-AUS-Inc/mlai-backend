"""Durable native financial settlement, independent of provider/rollout state."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_settlement as settlements
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError

ACTION = "cms-goal-refund"
FIELDS = {"schema_version", "goal_operation_id", "goal_id", "scope_digest", "retention_subject", "purge_after_reconciliation"}


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native article settlement changed.")


def reserve_refund(parent, *, now=None):
    """Snapshot accepted work before lifecycle erasure, without performing a credit."""
    if parent.state not in billing.records.REFUND_TERMINAL or not parent.receipt.get("first_article_billing"):
        return None
    if not transaction.get_connection().in_atomic_block:
        raise _deny()
    from .website_models import WebsiteConnectionOperation
    value = authority.historical(parent)
    billing.historical_reservation(parent)
    subject = settlements.retention_subject(billing.retention_subject(parent))
    payload = {"schema_version": 1, "goal_operation_id": str(parent.pk), "goal_id": value.goal["goal_id"],
        "scope_digest": digest(value.scope), "retention_subject": subject, "purge_after_reconciliation": False}
    row, _created = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{parent.connection_id}:cms-goal-refund:{parent.pk}", defaults={
            "connection": parent.connection, "generation": parent.generation, "action": ACTION, "state": "pending",
            "next_attempt_at": now or timezone.now(), "payload": payload, "receipt": {"status": "credit_pending"}})
    _validate(row, parent)
    return row


def _validate(row, parent):
    value = authority.historical(parent)
    payload = row.payload
    if (row.action != ACTION or parent.state not in billing.records.REFUND_TERMINAL or not isinstance(payload, dict) or
            set(payload) != FIELDS or type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
            type(payload["purge_after_reconciliation"]) is not bool or
            payload["goal_operation_id"] != str(parent.pk) or payload["goal_id"] != value.goal["goal_id"] or
            payload["scope_digest"] != digest(value.scope) or row.connection_id != parent.connection_id or row.generation != parent.generation):
        raise _deny()
    settlements.retention_subject(payload["retention_subject"])
    return value


def _record_settlement(parent, receipt, *, subject, now):
    from .dispatch_models import ContentFactoryDispatchOutbox
    outbox = ContentFactoryDispatchOutbox.objects.select_for_update().filter(
        client_request_id=receipt["client_request_id"], organization_id=parent.connection.organization_id).first()
    decision = "refunded" if receipt["status"] == "refunded" else "retained"
    if outbox is None:
        if decision == "retained":
            raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original native financial dispatch is still pending.", retryable=True)
        # An unused hold can be credited before a worker dispatch exists.
        return
    stamp = settlements.seal(outbox, decision=decision, subject=subject if decision == "retained" else None, at=now)
    if (stamp["goal_operation_id"] != str(parent.pk) or stamp["payer_user_id"] != receipt["payer_user_id"] or
            stamp["spend_ledger_id"] != receipt["spend_ledger_id"] or stamp["cost_points"] != receipt["cost_points"]):
        raise _deny()
    if settlements.REFERENCE in outbox.payload:
        prior = settlements.validate(outbox)
        if prior["decision"] == "refunded" and decision != "refunded":
            raise _deny()
        if prior["decision"] == decision:
            return
    outbox.payload = {**deepcopy(outbox.payload), settlements.REFERENCE: stamp}
    outbox.save(update_fields=["payload", "updated_at"])


def advance_refund(identifier, *, now=None):
    """Stop unused original CF work outside locks, then settle the held money."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    from . import cms_goal_cancellation as cancellation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None or candidate.state not in {"pending", "running"}:
        return None
    now, before, claimed, lease = now or timezone.now(), None, False, str(uuid4())
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if row is None or row.state not in {"pending", "running"} or row.next_attempt_at and row.next_attempt_at > now:
                return None
            before = row.attempts
            parent = WebsiteConnectionOperation.objects.select_for_update().filter(
                pk=row.payload.get("goal_operation_id"), action=authority.ACTION, connection_id=row.connection_id, generation=row.generation).first()
            if parent is None:
                raise _deny()
            _validate(row, parent)
            credited = billing.existing_credit(parent)
            subject = row.payload["retention_subject"] or billing.retention_subject(parent)
            needs_stop = credited is None and subject is None
            row.state, row.attempts, row.next_attempt_at = "running", before + 1, now + timedelta(minutes=2)
            row.receipt = {"status": "stopping_original_work" if needs_stop else "settling_original_hold", "lease_id": lease}
            row.save(update_fields=["state", "attempts", "next_attempt_at", "receipt", "updated_at"])
        claimed = True
        # Accepted drafts retain their original charge and CF handoff. Clearing
        # those artifacts would break GET-only uncertain-post reconciliation.
        stopped = cancellation.stop_unused(parent, now=now) if needs_stop else None
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION,
                state="running", receipt__lease_id=lease).first()
            if row is None:
                return None
            parent = WebsiteConnectionOperation.objects.select_for_update().filter(
                pk=row.payload["goal_operation_id"], action=authority.ACTION, connection_id=row.connection_id, generation=row.generation).first()
            if parent is None:
                raise _deny()
            _validate(row, parent)
            if needs_stop:
                original = cancellation.original(parent)
                if original is not None:
                    from .dispatch_models import ContentFactoryDispatchOutbox
                    original = ContentFactoryDispatchOutbox.objects.select_for_update().filter(pk=original.pk).first()
                    if original is None:
                        raise _deny()
                    proof = cancellation.validate(original, stopped)
                    original.payload = {**deepcopy(original.payload), cancellation.REFERENCE: proof}
                    original.state = "cancelled"
                    original.save(update_fields=["payload", "state", "updated_at"])
                elif stopped is not None:
                    raise _deny()
            receipt = billing.historical_reservation(parent)
            subject = row.payload["retention_subject"] or billing.retention_subject(parent)
            credited = billing.existing_credit(parent)
            if credited is not None:
                receipt = credited
            elif subject is None:
                receipt = billing.release(parent, reason="Native articles goal ended before generation.")
                if receipt["status"] != "refunded":
                    subject = billing.retention_subject(parent)
            _record_settlement(parent, receipt, subject=subject, now=now)
            row.state, row.next_attempt_at = "completed", None
            row.receipt = {"status": "credited" if receipt["status"] == "refunded" else "retained_after_draft",
                "goal_operation_id": str(parent.pk), "completed_at": now.isoformat()}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            if row.payload["purge_after_reconciliation"]:
                parent.payload, parent.receipt, parent.state, parent.next_attempt_at = {}, {"status": "financial_records_settled"}, "deleted", None
                parent.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return "completed"
    except Exception as exc:
        if before is None:
            raise
        permanent = not exc.retryable if isinstance(exc, WebsiteAuthorityError) else isinstance(exc, (ValueError, TypeError))
        state = "needs_attention" if permanent else "pending"
        owned = {"state": "running", "receipt__lease_id": lease} if claimed else {"state__in": ["pending", "running"], "attempts": before}
        WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(state=state, attempts=before + 1,
            next_attempt_at=None if permanent else now + timedelta(minutes=5), updated_at=now,
            receipt={"status": "credit_needs_review" if permanent else "credit_pending",
                "reason_code": "website_operation_changed" if permanent else "website_prepare_dependency_transient"})
        return state


def cancel_held_goals(connection, *, all_generations=False):
    """Retain useful-work proof and queue original credits before invalidating keys."""
    from .cms_setup_refunds import cancel_held_setups
    cancel_held_setups(connection, all_generations=all_generations)
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection, action=authority.ACTION,
        receipt__has_key="first_article_billing")
    if not all_generations:
        rows = rows.filter(generation=connection.generation)
    for parent in rows:
        # Completed native goals also need an earned-work audit before erasure;
        # their generated content is retained and their charge is not reversed.
        authority.historical(parent)
        parent.state, parent.next_attempt_at = "cancelled", None
        parent.payload = {key: item for key, item in parent.payload.items() if key != "lease_id"}
        parent.receipt = {**parent.receipt, "status": "authority_revoked", "user_action": None}
        parent.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])
        reserve_refund(parent)


def retain_for_erasure(website):
    """Erase native planning data while retaining only pending financial proof."""
    from .cms_setup_refunds import retain_for_erasure as retain_setups, erase_intent
    setup_pending = retain_setups(website)
    for parent in website.operations.filter(action=authority.ACTION, receipt__has_key="first_article_billing"):
        authority.historical(parent)
        billing.records._original_spend(billing.historical_reservation(parent), allow_refunded=True)
        erase_intent(parent)
        credit = website.operations.filter(action=ACTION, payload__goal_operation_id=str(parent.pk)).first()
        if credit is not None and credit.state == "completed":
            _validate(credit, parent)
            parent.payload, parent.receipt, parent.state, parent.next_attempt_at = {}, {"status": "financial_records_settled"}, "deleted", None
            parent.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            continue
        parent.payload = {**parent.payload, "generation_data": None, "capture_operation_id": None}
        parent.receipt = {key: parent.receipt[key] for key in ("goal", "first_article_billing") if key in parent.receipt}
        parent.save(update_fields=["payload", "receipt", "updated_at"])
    for row in website.operations.filter(action=ACTION):
        row.payload = {**row.payload, "purge_after_reconciliation": True}
        row.save(update_fields=["payload", "updated_at"])
    return setup_pending or website.operations.filter(action=ACTION).exclude(state="completed").exists()
