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
        return
    try:
        record(run)
    except Exception:
        # No provider errors, worker messages or request bodies in this log.
        # The original durable run remains available for a later observation.
        logger.warning("articles_usage_observation_incomplete")


def read(*, company_id, organization_id, goal_id=None, now=None):
    """Read retained company observations with bounded work and explicit coverage."""
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.filter(action=ACTION, state="completed",
        connection__organization_id=organization_id, payload__scope__company_id=str(company_id),
        payload__scope__organization_id=str(organization_id))
    if goal_id is not None:
        rows = rows.filter(payload__goal_id=goal_id)
    selected = list(rows.order_by("-updated_at", "-pk").values("payload", "receipt")[:MAX_READ + 1])
    result = costs.aggregate([row["receipt"] for row in selected[:MAX_READ]], now=now or timezone.now(), truncated=len(selected) > MAX_READ)
    return {**result, "companyId": str(company_id), "goalId": goal_id}
