"""Truthful original-goal owner observations; missing coverage never means zero."""

from copy import deepcopy
import logging
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from rest_framework.request import Request

from . import website_goal as goals

logger = logging.getLogger(__name__)
MAX_EVENTS = 512
GAPS = ("external_github_access_approval", "external_github_review", "external_github_merge",
    "external_github_push", "external_hosting_deploy", "uncovered_owner_endpoints", "successor_goal_lineage")


def enabled(organization_id):
    """Observe only the independently enabled doctor and enrolled goal cohort."""
    return bool(getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False) and goals.enrolled(organization_id))


def seed(op, *, from_start, now):
    """Persist an honest incomplete audit at birth, without retroactive zeroes."""
    if not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False) or not enabled(op.connection.organization_id):
        return
    op.receipt = {**op.receipt, "goal_audit": {"version": 1, "goal_id": str(op.pk),
        "started_at": now.isoformat(), "from_start": bool(from_start), "event_ids": [],
        "initial_setup_event": None, "overflow": False, "last_observed_at": None}}


def _valid(value, goal_id):
    return (isinstance(value, dict) and set(value) == {"version", "goal_id", "started_at", "from_start", "event_ids",
            "initial_setup_event", "overflow", "last_observed_at"}
        and type(value.get("version")) is int and value["version"] == 1
        and value.get("goal_id") == goal_id and type(value.get("from_start")) is bool
        and goals.stamp(value.get("started_at")) is not None and type(value.get("overflow")) is bool
        and isinstance(value.get("event_ids"), list) and len(value["event_ids"]) <= MAX_EVENTS
        and all(isinstance(item, str) and goals.DIGEST.fullmatch(item) for item in value["event_ids"])
        and len(set(value["event_ids"])) == len(value["event_ids"])
        and (value.get("last_observed_at") is None or goals.stamp(value["last_observed_at"]) is not None)
        and (value.get("initial_setup_event") is None or value["initial_setup_event"] in value["event_ids"]))


def public_audit(op):
    """Export a closed partial observation, never a fabricated exhaustive count."""
    raw = goals.mapping(op.receipt).get("goal_audit")
    valid = _valid(raw, str(op.pk))
    gaps = list(GAPS)
    if not valid or not raw["from_start"]:
        gaps.append("before_audit_started")
    if valid and raw["overflow"]:
        gaps.append("observation_capacity")
    return {"version": 1, "goal_id": str(op.pk), "basis": "original_goal_audit",
        "complete": False, "human_actions": None,
        "observed_owner_actions": len(raw["event_ids"]) - int(raw["initial_setup_event"] is not None) if valid else None,
        "coverage_gaps": gaps,
        "started_at": raw["started_at"] if valid else None,
        "observed_through": raw.get("last_observed_at") if valid else None}


def public_receipt(op, receipt):
    """Expose observations only with B7 and the goal cohort; hide the journal."""
    result = {key: value for key, value in receipt.items() if key != "goal_audit"}
    if not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
        return result
    if enabled(op.connection.organization_id) and "goal" in goals.mapping(op.receipt):
        result["goal_audit"] = public_audit(op)
    return result


def preserve_during_prepare_write(op, write):
    """Keep newer observations without invalidating the worker's existing lease."""
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=op.pk, action="prepare").first()
        if current is not None:
            saved = goals.mapping(current.receipt).get("goal_audit")
            if saved is not None:
                op.receipt = {**op.receipt, "goal_audit": deepcopy(saved)}
        return write()


def observe_owner_http(request, context, config, *, action, run_id=None, initial_setup=False, operation=None):
    """Record a deduplicated owner HTTP intent, excluding internal goal dispatch.

    This deliberately does not assert that HTTP intents enumerate human actions.
    Incomplete external and endpoint coverage stays visible in every projection.
    Optional observation failures cannot invalidate an already admitted action.
    """
    if (not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False)
            or not isinstance(request, Request) or not isinstance(getattr(request, "_request", None), HttpRequest)
            or getattr(request, "_website_prepare_dispatch", False)
            or not enabled(getattr(context.organization, "pk", None))):
        return
    from .website_models import WebsiteConnectionOperation
    from workflow_runs.models import ContentFactoryRun
    from .vibe_marketing_views import _run_belongs_to_context
    try:
        connection = config.website_connection
        if connection is None:
            return
        with transaction.atomic():
            rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection,
                generation=connection.generation, action="prepare", payload__company_id=str(context.company.pk))
            if operation is not None:
                rows = rows.filter(pk=operation.pk)
            elif run_id:
                run = ContentFactoryRun.objects.filter(run_id=run_id).first()
                if run is None or not _run_belongs_to_context(run, context):
                    return
                request_data = goals.mapping(run.run_request)
                original = request_data.get("first_article_goal_operation_id") or goals.mapping(request_data.get("goal_budget")).get("goal_id")
                if original:
                    rows = rows.filter(pk=original)
                else:
                    from django.db.models import Q
                    rows = rows.filter(Q(receipt__setup_run_id=run_id) | Q(receipt__child_run_id=run_id)
                        | Q(receipt__first_article_run_id=run_id) | Q(receipt__first_article_publish_run_id=run_id))
            op = rows.order_by("-created_at", "-pk").first()
            if op is None or op.state == "completed":
                return
            goal = goals.mapping(op.receipt).get("goal")
            scope = goals.scope_for(connection, context.company.pk)
            if not goals.valid_goal(goal, scope) or goal.get("goal_id") != str(op.pk):
                return
            raw = goals.mapping(op.receipt).get("goal_audit")
            now = timezone.now()
            if not _valid(raw, str(op.pk)):
                seed(op, from_start=False, now=now)
                raw = op.receipt["goal_audit"]
            raw = deepcopy(raw)
            identity = (request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")
                or request.data.get("client_request_id") or str(uuid4()))
            event = goals.digest({"kind": "owner_http_intent", "goal_id": str(op.pk),
                "action": str(action)[:100], "run_id": str(run_id or "")[:100],
                "actor_id": str(request.user.pk), "request_id": str(identity)[:1024]})
            if event in raw["event_ids"]:
                return
            if len(raw["event_ids"]) < MAX_EVENTS:
                raw["event_ids"].append(event)
                if initial_setup and raw["from_start"] and raw["initial_setup_event"] is None:
                    raw["initial_setup_event"] = event
            else:
                raw["overflow"] = True
            raw["last_observed_at"] = now.isoformat()
            op.receipt = {**op.receipt, "goal_audit": raw}
            # updated_at is the prepare worker's lease, not an audit timestamp.
            # Its final CAS preserves this journal under the same row lock.
            op.save(update_fields=["receipt"])
    except Exception:
        logger.warning("articles_goal_audit_observation_unavailable")
