"""Truthful original-goal owner observations; missing coverage never means zero."""

from copy import deepcopy
import hashlib
import json
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
MAX_INTENT_BYTES = 1024 * 1024
OUTSIDE_ORIGINAL_WINDOW = object()
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
    if from_start:
        from .website_owner_transport import admission
        transport = admission(op)
        if transport is not None:
            value = op.receipt["goal_audit"]
            value["transport"] = transport
            value["initial_setup_event"] = transport["initial_event"]
            value["event_ids"] = [transport["initial_event"]]
            value["last_observed_at"] = now.isoformat()


def _valid(value, goal_id):
    return (isinstance(value, dict) and set(value) - {"transport"} == {"version", "goal_id", "started_at", "from_start", "event_ids",
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
    transport = transport_coverage(op)
    if transport["coverage"] == "complete":
        gaps.remove("uncovered_owner_endpoints")
    return {"version": 1, "goal_id": str(op.pk), "basis": "original_goal_audit",
        "complete": False, "human_actions": None,
        "observed_owner_actions": len(raw["event_ids"]) - int(raw["initial_setup_event"] is not None) if valid else None,
        "coverage_gaps": gaps,
        "started_at": raw["started_at"] if valid else None,
        "observed_through": raw.get("last_observed_at") if valid else None}


def transport_coverage(op):
    """Close covered source paths only; independent request-window proof remains."""
    from .website_owner_transport import inventory, required
    unknown = {"version": 1, "basis": "original_goal_owner_transports_v1", "coverage": "partial",
        "gaps": ["owner_transport_origin_unobserved"], "manifest_digest": None,
        "goal_id": str(op.pk), "target": goals.mapping(op.receipt.get("goal")).get("target"),
        "scope_digest": goals.digest(goals.mapping(op.receipt.get("goal")).get("scope")),
        "from_start": False, "policy": "unknown", "observed_owner_actions": None,
        "started_at": None, "observed_through": None}
    raw = goals.mapping(op.receipt.get("goal_audit"))
    value = goals.mapping(raw.get("transport"))
    if (not _valid(raw, str(op.pk)) or set(value) != {"version", "manifest_digest", "origin", "from_start", "gaps", "initial_event", "required"}
            or type(value.get("version")) is not int or value["version"] != 1
            or raw.get("from_start") is not True or value.get("from_start") is not True
            or not isinstance(value.get("origin"), str) or value["origin"] not in {"http", "mcp"}
            or type(value.get("required")) is not bool
            or not isinstance(value.get("manifest_digest"), str) or not goals.DIGEST.fullmatch(value["manifest_digest"])
            or not isinstance(value.get("gaps"), list) or any(not isinstance(gap, str) for gap in value["gaps"])
            or not isinstance(value.get("initial_event"), str) or not goals.DIGEST.fullmatch(value["initial_event"])
            or value["initial_event"] != raw.get("initial_setup_event")):
        return unknown
    if value.get("required") is not True:
        return {**unknown, "gaps": ["owner_transport_best_effort"], "policy": "best_effort"}
    if not required(op.connection.organization_id):
        return {**unknown, "gaps": ["owner_transport_policy_not_enforced"], "policy": "unknown"}
    if raw["overflow"]:
        return {**unknown, "gaps": ["owner_transport_capacity"], "policy": "durable_required"}
    current = inventory()
    if current["manifest_digest"] != value["manifest_digest"] or current["coverage"] != "complete" or value["gaps"]:
        return {**unknown, "gaps": ["owner_transport_inventory_incomplete"]}
    return {**unknown, "coverage": "complete", "gaps": [], "from_start": True, "policy": "durable_required",
        "manifest_digest": current["manifest_digest"],
        "observed_owner_actions": len(raw["event_ids"]) - int(raw["initial_setup_event"] is not None),
        "started_at": raw["started_at"], "observed_through": op.updated_at.isoformat()}


def public_receipt(op, receipt):
    """Expose observations only with B7 and the goal cohort; hide the journal."""
    from .website_execution_observation import KEY
    result = {key: value for key, value in receipt.items() if key not in {"goal_audit", KEY}}
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


def append_observation(op, event, *, now, initial_setup=False):
    """Append a private event under the caller's original-operation row lock."""
    if not isinstance(event, str) or not goals.DIGEST.fullmatch(event):
        return
    raw = goals.mapping(op.receipt).get("goal_audit")
    if not _valid(raw, str(op.pk)):
        seed(op, from_start=False, now=now)
        raw = goals.mapping(op.receipt).get("goal_audit")
    if not _valid(raw, str(op.pk)):
        return
    raw = deepcopy(raw)
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
    op.save(update_fields=["receipt"])


def observe_owner_http(request, context, config, *, action, run_id=None, initial_setup=False, operation=None):
    """Record a deduplicated owner HTTP intent, excluding internal goal dispatch.

    This deliberately does not assert that HTTP intents enumerate human actions.
    Incomplete external and endpoint coverage stays visible in every projection.
    Optional observation failures cannot invalidate an already admitted action.
    """
    if (not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False)
            or not isinstance(request, Request) or not isinstance(getattr(request, "_request", None), HttpRequest)
            or getattr(request, "_website_prepare_dispatch", False)
            or getattr(request, "_articles_transport_observed", False)
            or not enabled(getattr(context.organization, "pk", None))):
        return
    if initial_setup and operation is not None:
        from .website_owner_transport import CURRENT, event_id
        observed = CURRENT.get()
        initial = goals.mapping(goals.mapping(operation.receipt.get("goal_audit")).get("transport"))
        if (isinstance(observed, dict) and observed.get("origin") == "http"
                and initial.get("initial_event") == event_id(observed, operation.pk)):
            # The initial action was committed with goal admission, before the
            # response or first advancement could be lost. Do not count it again.
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
            now = timezone.now()
            identity = (request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")
                or request.data.get("client_request_id") or str(uuid4()))
            if not isinstance(identity, str) or not 1 <= len(identity) <= 1024:
                return
            intent = json.dumps(dict(request.data), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(intent) > MAX_INTENT_BYTES:
                return
            event = goals.digest({"kind": "owner_http_intent", "goal_id": str(op.pk),
                "action": str(action)[:100], "run_id": str(run_id or "")[:100],
                "actor_id": str(request.user.pk), "request_id": identity,
                "intent_digest": hashlib.sha256(intent).hexdigest()})
            append_observation(op, event, now=now, initial_setup=initial_setup)
    except Exception:
        logger.warning("articles_goal_audit_observation_unavailable")


def observe_transport(context, config, observed):
    """Persist server-authenticated transport intent before domain transactions."""
    if not enabled(context.organization.pk) or transaction.get_connection().in_atomic_block:
        return False
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from workflow_runs.models import ContentFactoryRun
    from .vibe_marketing_views import _run_belongs_to_context
    from .website_owner_transport import event_id
    connection = config.website_connection
    if connection is None or observed["connection_id"] != str(connection.pk):
        return False
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        connection = WebsiteConnection.objects.select_for_update().select_related("organization").filter(
            pk=connection.pk, organization_id=context.organization.pk).first()
        if connection is None:
            return False
        rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection,
            generation=connection.generation, action="prepare", payload__company_id=str(context.company.pk))
        run_id = observed["run_id"]
        if run_id:
            run = ContentFactoryRun.objects.filter(run_id=run_id).first()
            if run is None or not _run_belongs_to_context(run, context):
                return False
            from .website_review_action_audit import original_goal
            op, _reason = original_goal(context, run)
            if op is None:
                latest = rows.order_by("-created_at", "-pk").first()
                saved = goals.mapping(run.run_request)
                claimed = any(key in saved for key in (
                    "first_article_goal_id", "first_article_goal_operation_id", "strategy_goal_id", "goal_budget"))
                if latest is not None and latest.state == "completed" and (not claimed or _reason == "terminal"):
                    return OUTSIDE_ORIGINAL_WINDOW
        else:
            op = rows.order_by("-created_at", "-pk").first()
        if op is None:
            return False if run_id else OUTSIDE_ORIGINAL_WINDOW
        if op.state == "completed":
            # Ordinary later publication belongs to its own request. Neither
            # reject it under the closed original goal's policy nor append it
            # retroactively to that first-article observation window.
            return OUTSIDE_ORIGINAL_WINDOW
        goal = goals.mapping(op.receipt).get("goal")
        if not goals.valid_goal(goal, goals.scope_for(connection, context.company.pk)) or goal.get("goal_id") != str(op.pk):
            return False
        event = event_id(observed, op.pk)
        append_observation(op, event, now=timezone.now())
        return event in goals.mapping(op.receipt.get("goal_audit")).get("event_ids", [])
