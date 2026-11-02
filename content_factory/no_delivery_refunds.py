"""Registry-owned no-delivery settlement and founder points outcomes (IF-P2-12).

Only a tenant-bound original SPEND can create a refund. Entitlements use their
existing release path; delivered or uncertain publication effects retain money.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .article_publication_evidence import publication_state, useful_delivery
from .billing_lineage import original_article_spend_for_run, paid_article_request
from .first_article_entitlement import entitled_request, release_entitlement
from .incident_guards import delivered_content
from . import registry_policy

TERMINAL_STATUSES = frozenset({"failed", "blocked", "cancelled", "denied", "stopped"})
OWNER_CANCEL_REASONS = frozenset({"owner_cancelled", "draft_deleted", "article_discarded"})
# Workflow steps that write or revise the article, rather than research/preflight.
WRITING_STEPS = frozenset({"write_article", "write_content", "draft_article", "generate_article", "compose_article",
                           "edit_article", "edit_content", "apply_component_feedback", "render_article"})
DELIVERY_EVIDENCE_KEYS = ("draft_ready_v1", "delivery_v1", "publication_evidence_v1")
SETTLED_STATES = ("captured", "refunded", "released", "kept", "none")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def enabled() -> bool:
    """Whether registry settlement is enabled; code default is off."""
    return bool(getattr(settings, "ARTICLES_REGISTRY_REFUNDS_ENABLED", False))


def points_receipt(run) -> dict:
    """Return only a normalized backend points receipt."""
    receipt = _mapping(_mapping(getattr(run, "result", None)).get("points_v1"))
    if type(receipt.get("version")) is not int or receipt.get("version") != 1 or receipt.get("state") not in {
        "reserved", "captured", "refunded", "released", "kept", "carried", "free", "none",
    }:
        return {}
    return dict(receipt)


def _failure(run):
    result = _mapping(run.result)
    return (_mapping(result.get("failure")).get("code") or result.get("error_code")
            or _mapping(result.get("authority_denied")).get("code"))


def _direct_delivery(run):
    result = _mapping(run.result)
    return (bool(result.get("draft_ready_v1")) or useful_delivery(run) or delivered_content(run)
            or publication_state(run) == "article" or points_receipt(run).get("state") == "captured")


def _delivery(run):
    """A publish-only continuation cannot undo delivery on its proven original charge."""
    if _direct_delivery(run):
        return True
    request = _mapping(getattr(run, "run_request", None))
    source_id = request.get("original_billing_source_run_id")
    if not source_id or not getattr(run, "organization_id", None):
        return False
    # Verify actual ledger/grant backing before trusting any sender-supplied lineage field.
    from .first_article_entitlement import lineage_entitlement
    grant = lineage_entitlement(run) if entitled_request(request) else None
    spend = _original_spend(run) if paid_article_request(request) else None
    if grant is None and spend is None:
        return False
    from workflow_runs.models import ContentFactoryRun
    seen = {run.run_id}
    for _ in range(16):
        if source_id in seen:
            return False
        seen.add(source_id)
        source = ContentFactoryRun.objects.filter(run_id=source_id, organization_id=run.organization_id,
            domain__iexact=run.domain).first()
        if source is None:
            return False
        saved = _mapping(source.run_request)
        if spend is not None:
            original = _original_spend(source)
            same = original is not None and original.pk == spend.pk
        else:
            original = lineage_entitlement(source) if entitled_request(saved) else None
            same = original is not None and original.pk == grant.pk
        if not same:
            return False
        if _direct_delivery(source):
            return True
        source_id = saved.get("original_billing_source_run_id")
        if not source_id:
            return False
    return False


def _writing_started(run):
    return run.steps.filter(step_key__in=WRITING_STEPS).filter(
        Q(started_at__isnull=False) | Q(status__in=["running", "completed", "failed", "blocked"])).exists()


def _original_spend(run):
    """Use original tenant-bound article or topic billing proof, including continuations."""
    article = original_article_spend_for_run(run)
    if article is not None:
        return article
    from .billing_lineage import original_spend_for_job
    from .models import ContentFactoryJob
    from workflow_runs.models import ContentFactoryRun
    from organizations.models import Organization
    if not Organization.objects.filter(pk=run.organization_id, domain__iexact=run.domain).exists():
        return None
    current, seen = run, set()
    for _ in range(16):
        if current.run_id in seen:
            return None
        seen.add(current.run_id)
        request = _mapping(current.run_request)
        if request.get("roo_points_action") != "content_island_topic_generation":
            return None
        ledger_id = str(request.get("roo_points_ledger_id") or "")
        if not ledger_id.isdecimal():
            return None
        job = ContentFactoryJob.objects.filter(domain__iexact=run.domain,
            client_request_id=request.get("client_request_id"), billing_ledger_id=int(ledger_id)).first()
        if job is not None:
            return original_spend_for_job(job)
        source_id = request.get("original_billing_source_run_id")
        if not source_id:
            return None
        current = ContentFactoryRun.objects.filter(run_id=source_id, organization_id=run.organization_id,
                                                   domain__iexact=run.domain).first()
        if current is None:
            return None
    return None


def _original_free_run(run):
    from workflow_runs.models import ContentFactoryRun
    current, seen = run, set()
    for _ in range(16):
        if current.run_id in seen:
            return None
        seen.add(current.run_id)
        request = _mapping(current.run_request)
        source_id = request.get("original_billing_source_run_id")
        if not source_id:
            return current if entitled_request(request) else None
        current = ContentFactoryRun.objects.filter(run_id=source_id, organization_id=run.organization_id).first()
        if current is None:
            return None
    return None


def refund_decision(run, *, now=None) -> dict:
    """Compute a settlement from current backend evidence, never a requested credit amount."""
    request, result = _mapping(run.run_request), _mapping(run.result)
    decision = {"action": "none", "why": "flag_off", "original_key": None, "points": 0}
    if not enabled():
        return decision
    previous = points_receipt(run)
    if previous.get("state") in {"refunded", "released", "kept"}:
        return {**decision, "why": "already_settled"}
    owner_cancel = result.get("cancellation_reason") in OWNER_CANCEL_REASONS and run.status == "cancelled"
    if _delivery(run):
        return {**decision, "action": "keep", "why": "owner_cancelled_after_delivery" if owner_cancel else "delivered"}
    if publication_state(run) == "unknown":
        return {**decision, "action": "keep", "why": "publication_unresolved"}
    if run.status not in TERMINAL_STATUSES:
        return {**decision, "why": "not_terminal"}
    if owner_cancel and _writing_started(run):
        return {**decision, "action": "keep", "why": "owner_cancelled_after_writing"}
    capped = bool(_mapping(result.get("self_heal_v1")).get("capped"))
    created_at = getattr(run, "created_at", None)
    wall = bool(created_at and (now or timezone.now()) - created_at >= timedelta(hours=48))
    pending = _mapping(result.get("self_heal_v1"))
    if pending.get("mode") == "policy" and pending.get("next_at") and not capped and not wall:
        return {**decision, "why": "retry_pending"}
    if not capped and not wall and not pending:
        from .p2_foundation import p2_applies
        if (getattr(settings, "ARTICLES_SELF_HEAL_POLICY_ENABLED", False) and p2_applies(run.organization)
                and registry_policy.failure_class(_failure(run), run.organization) in {
                    "transient", "repairable", "strategy", "waiting"}):
            return {**decision, "why": "policy_pending"}
    if request.get("first_article_goal_operation_id"):
        return {**decision, "why": "goal_settlement_owned"}
    if not (registry_policy.refund_on_no_delivery(_failure(run)) or capped or wall):
        return {**decision, "action": "keep", "why": "registry_keeps_charge"}
    if entitled_request(request):
        return {**decision, "action": "release", "why": "no_delivery"}
    if not paid_article_request(request):
        return {**decision, "why": "no_original_spend"}
    spend = _original_spend(run)
    if spend is None:
        return {**decision, "why": "no_original_spend"}
    return {"action": "refund", "why": "no_delivery", "original_key": spend.idempotency_key,
            "points": -int(spend.delta_microroo) // 1_000_000}


def _write_receipt(run, *, state, points=0, original_key=None, refund_ledger_id=None, now=None):
    from workflow_runs.models import ContentFactoryRun
    receipt = {"version": 1, "state": state, "points": points, "original_key": original_key,
               "at": (now or timezone.now()).isoformat(), "refund_ledger_id": refund_ledger_id}
    result = {**_mapping(run.result), "points_v1": receipt}
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result
    return receipt


def apply_refund_decision(run, decision, *, now=None) -> dict:
    """Settle once under locks, rechecking evidence and the actual debit before crediting."""
    from workflow_runs.models import ContentFactoryRun
    from roo.services import PointsService, IdempotencyConflictError
    if not enabled():
        return points_receipt(run)
    with transaction.atomic():
        locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        previous = points_receipt(locked)
        if previous.get("state") in {"refunded", "released", "kept"}:
            return previous
        if previous.get("state") == "captured" and not (
                locked.status == "cancelled" and _mapping(locked.result).get("cancellation_reason") in OWNER_CANCEL_REASONS):
            return previous
        current = refund_decision(locked, now=now)
        action = current["action"]
        if action == "refund":
            spend = _original_spend(locked)
            if spend is None:
                return _write_receipt(locked, state="none", now=now)
            try:
                row, _ = PointsService.refund(user=spend.user, delta=current["points"], source="CONTENT_FACTORY",
                    description="Article stopped without delivery", created_by_slack_id="system",
                    original_spend_key=spend.idempotency_key,
                    idempotency_key=f"no-delivery-refund:{spend.idempotency_key}")
            except IdempotencyConflictError:
                # A legacy path may have returned this exact debit already.
                row = spend.service_refunds.filter(user_id=spend.user_id, source="CONTENT_FACTORY",
                                                   delta_microroo=-spend.delta_microroo).first()
                if row is None:
                    raise
            receipt = _write_receipt(locked, state="refunded", points=current["points"],
                original_key=spend.idempotency_key, refund_ledger_id=row.pk, now=now)
            # Preserve the still-authoritative Job billing path and its dual-write signal.
            from .models import ContentFactoryJob
            for job in ContentFactoryJob.objects.filter(billing_ledger_id=spend.pk):
                job.billing_status = "refunded"
                job.save(update_fields=["billing_status"])
        elif action == "release":
            original = _original_free_run(locked)
            if original is None:
                return previous
            released = release_entitlement(organization=locked.organization_id, run=original, reason="no_delivery")
            if not released:
                from .first_article_entitlement import entitlement_payload
                if entitlement_payload(original).get("entitlementStatus") != "released":
                    return previous
            receipt = _write_receipt(locked, state="released", now=now)
        elif action == "keep":
            state = "captured" if current["why"] == "delivered" else "kept"
            # Unknown publishing effects remain pending until existing reconciliation
            # establishes an outcome. They must not become an irrevocable capture.
            if current["why"] == "publication_unresolved":
                return previous
            spend = _original_spend(locked) if paid_article_request(_mapping(locked.run_request)) else None
            receipt = _write_receipt(locked, state=state, points=-spend.delta_microroo // 1_000_000 if spend else 0,
                                     original_key=spend.idempotency_key if spend else None, now=now)
        elif current["why"] == "no_original_spend":
            receipt = _write_receipt(locked, state="none", now=now)
        else:
            return previous
    run.result = locked.result
    return receipt


def points_outcome(run) -> dict | None:
    """Return the founder-safe outcome only while this contract is enabled."""
    if not enabled():
        return None
    receipt = points_receipt(run)
    state = receipt.get("state")
    if state in {"free", "released", "none"} or entitled_request(_mapping(run.run_request)):
        return {"state": "no_charge", "points": None}
    if state == "refunded":
        return {"state": "refunded", "points": receipt.get("points")}
    if state in {"captured", "kept"}:
        return {"state": "charged" if state == "captured" else "kept", "points": receipt.get("points")}
    return {"state": "pending", "points": None}


def cancel_outcome(run) -> dict:
    """Explain the points consequence before a founder confirms cancellation."""
    free = entitled_request(_mapping(run.run_request))
    if free:
        return {"state": "no_charge", "points": None, "freeArticle": True}
    spend = _original_spend(run) if paid_article_request(_mapping(run.run_request)) else None
    return {"state": "no_charge" if spend is None else "kept" if _delivery(run) or _writing_started(run) else "refund",
            "points": -spend.delta_microroo // 1_000_000 if spend else None, "freeArticle": False}


def carry_charge(*, source_run, new_run) -> dict:
    """Carry the backend's existing charge/entitlement to a continuation without a new debit."""
    from workflow_runs.models import ContentFactoryRun
    if source_run.organization_id != new_run.organization_id or source_run.pk == new_run.pk:
        raise ValueError("A charge needs a distinct continuation in the same startup.")
    with transaction.atomic():
        locked = {row.pk: row for row in ContentFactoryRun.objects.select_for_update().filter(
            pk__in=[source_run.pk, new_run.pk]).order_by("pk")}
        source, target = locked[source_run.pk], locked[new_run.pk]
        if source.organization_id != target.organization_id:
            raise ValueError("A charge cannot move between startups.")
        spend = _original_spend(source) if paid_article_request(_mapping(source.run_request)) else None
        if spend and spend.service_refunds.exists():
            raise ValueError("A refunded charge cannot fund another run.")
        saved, incoming = _mapping(source.run_request), _mapping(target.run_request)
        if spend is None:
            from .first_article_entitlement import entitlement_payload
            if not entitled_request(saved) or entitlement_payload(source).get("entitlementStatus") not in {"reserved", "consumed"}:
                raise ValueError("The source has no available charge or entitlement.")
        candidate = incoming.get("roo_points_ledger_id")
        if candidate not in (None, "") and str(candidate) != str(saved.get("roo_points_ledger_id")):
            raise ValueError("The continuation already has a different charge.")
        request = {**incoming, **{key: value for key, value in saved.items() if key.startswith("roo_points_")},
                   "original_billing_source_run_id": saved.get("original_billing_source_run_id") or source.run_id}
        ContentFactoryRun.objects.filter(pk=target.pk).update(run_request=request)
        receipt = _write_receipt(target, state="carried", points=-spend.delta_microroo // 1_000_000 if spend else 0,
                                original_key=spend.idempotency_key if spend else None)
    new_run.run_request, new_run.result = request, target.result
    return receipt


def run_refund_sweep(now=None, limit=None) -> dict:
    """Settle a bounded batch of terminal records; do nothing with the flag off."""
    if not enabled():
        return {"skipped": "flag_off"}
    from workflow_runs.models import ContentFactoryRun
    limit = max(1, min(int(limit or getattr(settings, "ARTICLES_SELF_HEAL_SWEEP_LIMIT", 50)), 500))
    query = ContentFactoryRun.objects.filter(status__in=TERMINAL_STATUSES).filter(
        ~Q(result__has_key="points_v1") | ~Q(result__points_v1__state__in=SETTLED_STATES)).order_by("pk")
    result = {"processed": 0, "refunded": 0, "released": 0}
    remaining = []
    # No-debit records need no ledger operation. Write their safe receipts in a
    # locked batch, so the common 500-organization sweep has bounded queries.
    with transaction.atomic():
        plain = []
        for run in list(query.select_for_update()[:limit]):
            request = _mapping(run.run_request)
            if (not paid_article_request(request) and not entitled_request(request)
                    and not _delivery(run) and publication_state(run) != "unknown"
                    and _mapping(run.result).get("cancellation_reason") not in OWNER_CANCEL_REASONS):
                decision = refund_decision(run, now=now)
                if decision["why"] == "no_original_spend":
                    run.result = {**_mapping(run.result), "points_v1": {"version": 1, "state": "none", "points": 0,
                        "original_key": None, "at": (now or timezone.now()).isoformat(), "refund_ledger_id": None}}
                    plain.append(run)
                    continue
            remaining.append(run)
        if plain:
            ContentFactoryRun.objects.bulk_update(plain, ["result"], batch_size=500)
        result["processed"] = len(plain)
    for run in remaining:
        decision = refund_decision(run, now=now)
        receipt = apply_refund_decision(run, decision, now=now)
        result["processed"] += 1
        if receipt.get("state") in {"refunded", "released"}:
            result[receipt["state"]] += 1
    return result
