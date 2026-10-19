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


def wake_settlement(*, parent_id, organization_id, child_run_id=None, dispatch_id=None, publication_intent_id=None, now=None):
    """Requeue an existing obligation for new bound evidence without buying retries.

    Call only after the child/dispatch transaction commits: settlement locks the
    parent before either child store, so waking inside a child lock would invert
    that order. This method neither contacts a provider nor credits points.
    """
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from .website_first_article_billing import _reservation
    from .website_first_article_settlement import MAX_CHILDREN, NO_WORK, _owned, _saved, _binding
    from .article_publication_evidence import cancellation_receipt
    from .dispatch_models import ContentFactoryDispatchOutbox
    candidate = WebsiteConnectionOperation.objects.filter(pk=parent_id, action="prepare",
        connection__organization_id=organization_id).values("connection_id").first()
    if candidate is None:
        return
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization_id)
        WebsiteConnection.objects.select_for_update().get(pk=candidate["connection_id"])
        row = WebsiteConnectionOperation.objects.select_for_update().filter(action=ACTION,
            connection_id=candidate["connection_id"], payload__goal_operation_id=str(parent_id)).first()
        if row is None or row.state != "needs_attention":
            return
        parent = WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(
            pk=parent_id, action="prepare", connection_id=row.connection_id, generation=row.generation).first()
        if (parent is None or parent.state not in REFUND_TERMINAL
            or goals.mapping(parent.receipt.get("goal")).get("goal_id") != row.payload.get("goal_id")):
            return
        try:
            billing = _reservation(parent, goals.mapping(parent.receipt.get("goal")))
            previous = _saved(parent, _binding(parent, billing))
            if billing["status"] == "refunded" or not previous:
                return
            if child_run_id:
                child = ContentFactoryRun.objects.select_for_update().filter(run_id=child_run_id, organization_id=organization_id).first()
                if child is None:
                    return
                lineage = {child.run_id: child}
                source = goals.mapping(child.run_request).get("source_run_id")
                while source and source not in lineage and len(lineage) < MAX_CHILDREN:
                    ancestor = ContentFactoryRun.objects.select_for_update().filter(run_id=source, organization_id=organization_id).first()
                    if ancestor is None:
                        break
                    lineage[ancestor.run_id] = ancestor
                    source = goals.mapping(ancestor.run_request).get("source_run_id")
                if not _owned(child, parent, billing, lineage):
                    return
                if publication_intent_id:
                    from .article_publication_evidence import _checked_publication, publication_state
                    from .article_publication_intent import HASH
                    if (not isinstance(publication_intent_id, str) or not HASH.fullmatch(publication_intent_id)
                            or child.result.get("publication_observation_intent_id") != publication_intent_id
                            or publication_state(child) != "article"):
                        return
                    proof = _checked_publication(child, child.result.get("publication_evidence_v1"))
                    event = goals.digest({"intent_id": publication_intent_id, "publication": proof})
                else:
                    proof = cancellation_receipt(child, goals.mapping(child.result))
                    if (proof is None or child.status != "cancelled" or proof["outcome"] != "no_article_effect"
                        or proof["delivered_value"] is None or any(item["run_id"] == child.run_id
                            and item.get("stop_receipt_id") == proof["receipt_id"] for item in previous["children"])):
                        return
                    event = proof["receipt_id"]
            elif dispatch_id:
                dispatch = ContentFactoryDispatchOutbox.objects.select_for_update().filter(pk=dispatch_id,
                    organization_id=organization_id, client_request_id=billing["client_request_id"]).first()
                request = goals.mapping(goals.mapping(dispatch.payload).get("request")) if dispatch else {}
                if (request.get("first_article_goal_operation_id") != str(parent.pk)
                    or request.get("first_article_goal_id") != str(parent.pk)
                    or request.get("client_request_id") != billing["client_request_id"]):
                    return
                state = ("delivered" if dispatch.state == "delivered" and dispatch.run_id else
                    "no_work" if dispatch.state == "failed" and not dispatch.run_id and dispatch.last_error in NO_WORK else "unknown")
                if state == "unknown" or previous["dispatch"] == state:
                    return
                event = goals.digest({"dispatch": str(dispatch.pk), "state": state, "run_id": dispatch.run_id})
            else:
                return
        except (WebsiteAuthorityError, ValueError, TypeError, KeyError):
            return
        if row.receipt.get("wake_evidence") == event:
            return
        row.state, row.next_attempt_at = "pending", now or timezone.now()
        row.receipt = {**row.receipt, "wake_evidence": event}
        row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])


def wake_dispatch_settlement(identifier):
    """Observe a committed terminal dispatch result for its original goal only."""
    from .dispatch_models import ContentFactoryDispatchOutbox
    row = ContentFactoryDispatchOutbox.objects.filter(pk=identifier).first()
    request = goals.mapping(goals.mapping(row.payload).get("request")) if row else {}
    if request.get("first_article_goal_operation_id"):
        wake_settlement(parent_id=request["first_article_goal_operation_id"], organization_id=row.organization_id,
            dispatch_id=row.pk)


def advance_refund(identifier, *, now=None):
    """Settle only the original goal/ledger; database and acknowledgment loss replay safely."""
    from .website_models import WebsiteConnectionOperation
    from .website_models import WebsiteConnection
    from organizations.models import Organization
    from .website_first_article_settlement import KEY, stop_children
    now = now or timezone.now()
    before = None
    evidence = None
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).values(
        "connection_id", "connection__organization_id").first()
    if candidate is None:
        return None
    try:
        # Settlement uses database-only PointsService operations. It is safe to
        # hold these locks; no provider call or fresh credential is involved.
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate["connection__organization_id"])
            WebsiteConnection.objects.select_for_update().get(pk=candidate["connection_id"])
            row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if (row is None or row.state not in {"pending", "running"}
                or row.next_attempt_at is not None and row.next_attempt_at > now):
                return None
            wake_evidence = row.receipt.get("wake_evidence")
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
            evidence = goals.mapping(parent.receipt.get(KEY))
            if receipt["status"]=="refunded":
                reserve_replacement_after_credit(parent,now=now)
            elif evidence.get("status")=="retained":
                reserve_replacement_after_credit(parent,now=now,retained=True)
            outcome = "credited" if receipt["status"] == "refunded" else evidence.get("status", "needs_reconciliation")
            if outcome in {"credited", "retained"}:
                row.state, row.next_attempt_at = "completed", None
                row.receipt = {"status": "credited" if outcome == "credited" else "retained_after_draft",
                    "goal_operation_id": str(parent.pk), "completed_at": now.isoformat()}
            else:
                # A pending dispatch or a typed unknown effect needs read-only
                # reconciliation, never repeated cancellation or a timed credit.
                retry = any(item.get("state") == "stop_required" for item in evidence.get("children", [])) and row.attempts <= 3
                row.state = "pending" if retry else "needs_attention"
                row.next_attempt_at = now + timedelta(minutes=5) if retry else None
                row.receipt = {"status": "stop_pending" if retry else "credit_needs_reconciliation",
                    "goal_operation_id": str(parent.pk), "settlement_digest": evidence.get("digest")}
            if wake_evidence:
                row.receipt["wake_evidence"] = wake_evidence
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            state = row.state
        if state == "pending" and evidence:
            stop_children(evidence, parent_id=parent.pk)
        if state == "completed":
            try:
                from .website_sandbox_faults import observe_budget_settlement
                observe_budget_settlement(parent, row)
            except Exception:
                # An optional acceptance receipt cannot undo a committed credit
                # or turn it into another financial attempt.
                pass
        return state
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
        from .website_first_article_billing import _reservation
        from .website_first_article_settlement import KEY, capture
        capture(held, _reservation(held, goals.mapping(held.receipt.get("goal"))), erasing=True)
        held.payload={key:held.payload[key] for key in ("company_id","requested_by_user_id") if key in held.payload}
        held.receipt={key:held.receipt[key] for key in ("goal","first_article_billing",KEY) if key in held.receipt}
        held.save(update_fields=["payload","receipt","updated_at"])
    for credit in website.operations.filter(action=ACTION):
        credit.payload={**credit.payload,"purge_after_reconciliation":True}
        credit.save(update_fields=["payload","updated_at"])
    return website.operations.filter(action=ACTION).exclude(state="completed").exists()
