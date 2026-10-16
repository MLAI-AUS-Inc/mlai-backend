"""Durable first-article credits that survive cancellation and disabled rollouts."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_first_article_billing import REFUND_TERMINAL, release

ACTION = "goal-refund"


def reserve_refund(op, *, now=None):
    """Queue settlement with the winning terminal state, without delaying revocation."""
    if op.state not in REFUND_TERMINAL or not op.receipt.get("first_article_billing"):
        return None
    from .website_models import WebsiteConnectionOperation
    goal = goals.mapping(op.receipt.get("goal"))
    row, _ = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{op.connection.pk}:goal-refund:{op.pk}", defaults={
            "connection":op.connection, "generation":op.generation, "action":ACTION,
            "state":"pending", "next_attempt_at":now or timezone.now(),
            "payload":{"goal_operation_id":str(op.pk), "goal_id":goal.get("goal_id")},
            "receipt":{"status":"credit_pending"}})
    return row


def advance_refund(identifier, *, now=None):
    """Settle only the original goal/ledger; database and acknowledgment loss replay safely."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    before = None
    try:
        # Settlement uses database-only PointsService operations. It is safe to
        # hold these locks; no provider call or fresh credential is involved.
        with transaction.atomic():
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if (row is None or row.state not in {"pending", "running"}
                or row.next_attempt_at is not None and row.next_attempt_at > now):
                return None
            before = row.attempts
            row.state, row.attempts = "running", before + 1
            row.next_attempt_at = now + timedelta(minutes=2)
            row.save(update_fields=["state", "attempts", "next_attempt_at", "updated_at"])
            parent = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
                pk=row.payload.get("goal_operation_id"), action="prepare", connection_id=row.connection_id,
                generation=row.generation).first()
            if (parent is None or goals.mapping(parent.receipt.get("goal")).get("goal_id") != row.payload.get("goal_id")
                or parent.state not in REFUND_TERMINAL):
                raise WebsiteAuthorityError("website_operation_changed", "The original article refund goal has changed.")
            receipt = release(parent, reason="Articles goal ended before publication.")
            if receipt is None:
                raise WebsiteAuthorityError("website_operation_changed", "The original article refund receipt is unavailable.")
            from .website_first_article_billing import reserve_replacement_after_credit
            if receipt["status"]=="refunded":
                reserve_replacement_after_credit(parent,now=now)
            row.state, row.next_attempt_at = "completed", None
            row.receipt = {"status":"credited" if receipt["status"] == "refunded" else "retained_after_draft",
                "goal_operation_id":str(parent.pk), "completed_at":now.isoformat()}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return "completed"
    except Exception as exc:
        if before is None:
            raise
        permanent = isinstance(exc, (WebsiteAuthorityError, ValueError, TypeError))
        state = "needs_attention" if permanent else "pending"
        # The failed atomic transaction rolled back its lease and credit. A
        # committed-but-unacknowledged transaction cannot match this old attempt.
        WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, attempts=before,
            state__in=["pending", "running"]).update(state=state, attempts=before+1,
                next_attempt_at=None if permanent else now+timedelta(minutes=5), updated_at=now,
                receipt={"status":"credit_needs_review" if permanent else "credit_pending",
                    "reason_code":"website_operation_changed" if permanent else "website_prepare_dependency_transient"})
        return state


def finish_purged_connection(connection_id):
    """Erase a revoked website shell only after its financial outbox is settled."""
    from organizations.models import Organization
    from .website_models import WebsiteConnection
    candidate = WebsiteConnection.objects.filter(pk=connection_id).first()
    if candidate is None:
        return
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=candidate.organization_id)
        candidate = WebsiteConnection.objects.select_for_update().filter(pk=connection_id,
            authorized_by__isnull=True, state="revoked").first()
        if (candidate and not candidate.operations.filter(state="pending").exists()
            and not candidate.operations.filter(action=ACTION).exclude(state="completed").exists()):
            candidate.delete()


def retain_financial_refunds_for_erasure(website):
    """Retain scoped ledger proof, erase general run data, and tag eventual cleanup."""
    from django.db.models import Q
    financial = Q(action=ACTION) | Q(action="prepare", receipt__has_key="first_article_billing")
    website.operations.filter(Q(action__in=["cleanup", "worker_followup", "superseded-setup-cleanup"]) | ~Q(state="pending")).exclude(financial).delete()
    for held in website.operations.filter(action="prepare", receipt__has_key="first_article_billing"):
        held.payload={key:held.payload[key] for key in ("company_id","requested_by_user_id") if key in held.payload}
        held.receipt={key:held.receipt[key] for key in ("goal","first_article_billing") if key in held.receipt}
        held.save(update_fields=["payload","receipt","updated_at"])
    for credit in website.operations.filter(action=ACTION):
        credit.payload={**credit.payload,"purge_after_reconciliation":True}
        credit.save(update_fields=["payload","updated_at"])
    return website.operations.filter(action=ACTION).exclude(state="completed").exists()
