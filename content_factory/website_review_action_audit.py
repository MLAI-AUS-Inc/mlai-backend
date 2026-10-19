"""Bounded partial observations of authenticated review mutation intents."""
import hashlib
import json
import logging
import re
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from rest_framework.request import Request

from . import website_goal as goals, website_goal_audit as audit

logger = logging.getLogger(__name__)
REPLAY_ACTIONS = {"editText", "editTextBatch", "regenerateImage", "chooseImage", "undo", "allowAI"}
REFERENCE_FIELDS = ("setup_run_id", "child_run_id", "first_article_run_id", "first_article_publish_run_id")
GOAL_ALIASES = ("first_article_goal_id", "strategy_goal_id")
MAX_INTENT_BYTES = 1024 * 1024


def intent(data):
    """Use only identities supported by the actual review mutation protocol."""
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action in {"applyUpdate", "applyReview"}:
        from .article_review_views import normalize_review_update
        value = normalize_review_update(data)
        identity = value["operationId"]
    elif action in REPLAY_ACTIONS:
        # Identity syntax is locally validated; field/content semantics remain
        # the service's job. A later semantic rejection is still an observed
        # authenticated mutation attempt, never proof of a completed human edit.
        identity = data.get("operationId")
        if (not isinstance(identity, str) or not re.fullmatch(r"[\w-]{8,100}", identity)
                or not isinstance(data.get("expectedRevision"), str)
                or not 1 <= len(data["expectedRevision"]) <= 200):
            return None
        value = data
    elif action == "discardImage":
        if any(not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= 255
                for key in ("fieldId", "candidateId")):
            return None
        # This producer has no operation replay contract. A repeated request is
        # another observed intent, even with the same generic idempotency header.
        identity, value = str(uuid4()), data
    else:
        # GET and refresh may be automatic polling/recovery, not owner intent.
        return None
    value = {key: value for key, value in value.items() if key != "expectedRevision"}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_INTENT_BYTES:
        return None
    return {"action": value["action"], "operation": identity, "fingerprint": hashlib.sha256(encoded).hexdigest()}


def original_goal(context, run):
    """Resolve exactly one owned parent under locks; never choose a latest goal."""
    from .models import OrganizationContentConfig
    from .website_connections import contract_for
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    if run.organization_id != context.organization.pk:
        return None, "run_owner"
    config = OrganizationContentConfig.objects.filter(organization=context.organization).first()
    if config is None or config.website_connection_id is None:
        return None, "connection"
    connection = WebsiteConnection.objects.select_for_update().select_related("organization").filter(
        pk=config.website_connection_id, organization=context.organization).first()
    saved = goals.mapping(run.run_request)
    if connection is None or connection.state != "connected":
        return None, "connection"
    if (run.github_repo != connection.github_repo or run.domain != connection.organization.domain
            or any(type(saved.get(key)) is not type(value) or saved.get(key) != value
                for key, value in contract_for(connection).items())):
        return None, "run_scope"
    budget = goals.mapping(saved.get("goal_budget"))
    explicit = [saved[key] for key in ("first_article_goal_operation_id",) if key in saved]
    if "goal_id" in budget:
        explicit.append(budget["goal_id"])
    # Other producer goal IDs must agree, but cannot supply a new fallback path.
    claimed = [*explicit, *(saved[key] for key in GOAL_ALIASES if key in saved)]
    if (any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in claimed)
            or len(set(claimed)) > 1):
        return None, "lineage"
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection=connection,
        generation=connection.generation, action="prepare", payload__company_id=str(context.company.pk))
    if explicit:
        rows = rows.filter(pk=explicit[0])
    else:
        matches = Q()
        for field in REFERENCE_FIELDS:
            matches |= Q(**{"receipt__" + field: run.run_id})
        rows = rows.filter(matches)
    candidates = list(rows[:2])
    if len(candidates) != 1:
        return None, "lineage"
    op = candidates[0]
    if (op.connection_id != connection.pk or op.generation != connection.generation or op.action != "prepare"
            or goals.mapping(op.payload).get("company_id") != str(context.company.pk)
            or any(str(op.pk) != value for value in claimed)
            or (not explicit and not any(goals.mapping(op.receipt).get(field) == run.run_id for field in REFERENCE_FIELDS))):
        return None, "lineage"
    goal = goals.mapping(op.receipt).get("goal")
    if not goals.valid_goal(goal, goals.scope_for(connection, context.company.pk)) or goal["goal_id"] != str(op.pk):
        return None, "goal_scope"
    if op.state not in {"pending", "running", "needs_user"}:
        return None, "terminal"
    if ("goal_budget" in saved and (type(budget.get("version")) is not int or budget["version"] != 1
            or budget.get("scope_digest") != goals.digest(goal["scope"]))):
        return None, "goal_scope"
    source = saved.get("expected_source_sha")
    if (not isinstance(source, str) or not goals.SHA.fullmatch(source)
            or source != (goal.get("current_source_sha") or connection.verified_sha)):
        return None, "source"
    return op, None


def observe_review(request, context, run, data):
    """Record an owned mutation attempt before dispatch without changing its result.

    This is an authenticated principal's observed intent, not proof of a human
    origin or exhaustive coverage. Unattributable/late attempts retain fixed
    diagnostics and the existing incomplete coverage gaps; no goal is invented.
    """
    if not eligible(request, context, methods={"POST"}):
        return
    try:
        observed = intent(data)
        if observed is not None:
            record_intent(request, context, run, observed, kind="review_http_intent", methods={"POST"})
    except (ValueError, TypeError):
        logger.info("articles_review_action_audit_unavailable reason=invalid_intent")
    except Exception:
        logger.warning("articles_review_action_audit_observation_unavailable")


def eligible(request, context, *, methods):
    """Check the existing server-owned HTTP and enrollment boundaries."""
    return not (not audit.enabled(getattr(getattr(context, "organization", None), "pk", None))
            or not isinstance(request, Request) or not isinstance(getattr(request, "_request", None), HttpRequest)
            or request.method not in methods or getattr(request, "_website_prepare_dispatch", False)
            or getattr(request, "_articles_transport_observed", False)
            or not request.user.is_authenticated or request.user.pk is None)


def record_intent(request, context, run, observed, *, kind, methods):
    """Append one server-validated intent using the shared exact original parent."""
    if not eligible(request, context, methods=methods):
        return
    try:
        if (kind not in {"review_http_intent", "comment_http_intent"}
                or not isinstance(observed, dict) or set(observed) != {"action", "operation", "fingerprint"}
                or any(not isinstance(observed.get(key), str) or not 1 <= len(observed[key]) <= 100
                    for key in ("action", "operation"))
                or not isinstance(observed.get("fingerprint"), str)
                or not goals.DIGEST.fullmatch(observed["fingerprint"])):
            return
        with transaction.atomic():
            op, reason = original_goal(context, run)
            if op is None:
                logger.info("articles_review_action_audit_unavailable reason=%s", reason)
                return
            event = goals.digest({"kind": kind, "goal_id": str(op.pk),
                "run_id": run.run_id, "actor_id": str(request.user.pk), **observed})
            audit.append_observation(op, event, now=timezone.now())
    except (ValueError, TypeError):
        logger.info("articles_review_action_audit_unavailable reason=invalid_intent")
    except Exception:
        logger.warning("articles_review_action_audit_observation_unavailable")
