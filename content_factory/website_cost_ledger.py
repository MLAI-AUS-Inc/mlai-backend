"""Durable original-run accounting, isolated from setup authority and bootstrap."""
import logging
from uuid import UUID
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import website_cost_usage as costs, website_goal as goals

ACTION = "goal-usage"
MAX_READ = 2000
logger = logging.getLogger(__name__)


def enabled():
    """Accounting is an independent, default-off server rollout policy."""
    return getattr(settings, "ARTICLES_COST_ACCOUNTING_ENABLED", False)


def record(run, *, now=None):
    """Read original attribution, then serialize snapshots on the unique run row."""
    from .website_models import WebsiteConnectionOperation
    saved = goals.mapping(run.run_request)
    try:
        identifier = UUID(str(goals.mapping(saved.get("goal_budget")).get("goal_id")))
    except (ValueError, TypeError, AttributeError):
        return None
    with transaction.atomic():
        parent = WebsiteConnectionOperation.objects.select_related("connection").filter(
            pk=identifier, action="prepare", connection__organization_id=run.organization_id).first()
        if parent is None:
            return None
        metadata = costs.attribution(parent, run)
        # The table's idempotency key is globally unique, including across goals
        # which reuse the same original worker run.
        key = "articles:usage:" + goals.digest({"run": run.run_id})
        row, _ = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "connection_id": parent.connection_id, "generation": parent.generation, "action": ACTION,
            "state": "completed", "payload": metadata, "receipt": {}})
        row = WebsiteConnectionOperation.objects.select_for_update().get(pk=row.pk)
        if (row.action != ACTION or row.connection_id != parent.connection_id or row.generation != parent.generation or row.payload != metadata):
            raise ValueError("Accounting run attribution changed")
        current = costs.merge(row.receipt, goals.mapping(run.result).get("usage"), run_id=run.run_id, now=now or timezone.now())
        if current != row.receipt:
            row.receipt = current
            row.save(update_fields=["receipt", "updated_at"])
        return row


def observe(run):
    """Accounting outages cannot turn into an owner action or stop site setup."""
    if not enabled():
        return None
    try:
        return record(run) is not None
    except Exception:
        # No provider errors, worker messages or request bodies in this log.
        # The original durable run remains available for a later observation.
        logger.warning("articles_usage_observation_incomplete")
        return False


def read(*, company_id, organization_id, goal_id=None, now=None):
    """Read retained company observations with bounded work and explicit coverage."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.filter(action=ACTION, state="completed",
        connection__organization_id=organization_id, payload__scope__company_id=str(company_id),
        payload__scope__organization_id=str(organization_id))
    if goal_id is not None:
        rows = rows.filter(payload__goal_id=goal_id)
    from .website_cost_recovery import ACTION as recovery_action, identifiers
    recovery = WebsiteConnectionOperation.objects.filter(action=recovery_action,
        state__in=["pending", "running", "needs_attention"], connection__organization_id=organization_id,
        payload__scope__company_id=str(company_id), payload__scope__organization_id=str(organization_id))
    if goal_id is not None:
        recovery = recovery.filter(payload__goal_operation_id=goal_id)
    batches = list(recovery.order_by("-updated_at", "-pk").values_list("payload", flat=True)[:MAX_READ + 1])
    # Read pending work first: if recovery completes between these reads, its
    # older ledger snapshot must still be shown as incomplete for this response.
    selected = list(rows.order_by("-updated_at", "-pk").values("payload", "receipt")[:MAX_READ + 1])
    pending, incomplete = set(), False
    truncated = len(selected) > MAX_READ or len(batches) > MAX_READ
    for batch in batches[:MAX_READ]:
        try:
            pending.update(identifiers(goals.mapping(batch).get("run_ids")))
        except ValueError:
            incomplete = True
        if len(pending) > MAX_READ:
            truncated = True
            pending = set(sorted(pending)[:MAX_READ])
            break
    records, recorded = [], set()
    for item in selected[:MAX_READ]:
        identifier = goals.mapping(item["payload"]).get("run_id")
        record = dict(goals.mapping(item["receipt"]))
        if isinstance(identifier, str) and identifier in pending:
            record["incomplete_reason"] = "observation_pending"
        records.append(record)
        if isinstance(identifier, str):
            recorded.add(identifier)
    for identifier in sorted(pending - recorded):
        if len(records) == MAX_READ:
            truncated = True
            break
        records.append({"usage": None, "incomplete_reason": "observation_pending"})
    result = costs.aggregate(records, now=now or timezone.now(), truncated=truncated, incomplete=incomplete or bool(pending))
    return {**result, "companyId": str(company_id), "goalId": goal_id, "pendingObservations": len(pending)}
