"""Native first-article recovery resolves its original outbox before any start."""

from copy import deepcopy

from django.conf import settings

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_budget as budget
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError


def original_dispatch(operation):
    """Read only the original held request; random analytics IDs are never rebuilt."""
    from .dispatch_models import ContentFactoryDispatchOutbox
    from .channel_artifacts import authorize_payload
    value = authority.historical(operation)
    held = billing.historical_reservation(operation)
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=held["client_request_id"],
        organization_id=value.scope["binding"]["organization_id"]).first()
    if row is None:
        return None
    payload = row.payload.get("request") if isinstance(row.payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("cms_goal_operation_id") != str(operation.pk) or
            payload.get("cms_goal_id") != value.goal["goal_id"] or payload.get("client_request_id") != held["client_request_id"] or
            str(payload.get("roo_points_billing_user_id")) != held["payer_user_id"] or payload.get("roo_points_cost") != held["cost_points"] or
            payload.get("roo_points_ledger_id") != held["spend_ledger_id"] or
            authorize_payload(payload).scope.binding != value.scope["binding"]):
        raise WebsiteAuthorityError("website_operation_changed", "The original native first-article dispatch changed.")
    return row


def original_run(operation, dispatch=None):
    """Resolve a provisional or real run by immutable original backend request."""
    from workflow_runs.models import ContentFactoryRun
    row = dispatch or original_dispatch(operation)
    if row is None:
        return None
    original = row.payload["request"]
    runs = ContentFactoryRun.objects.filter(organization_id=operation.connection.organization_id,
        run_request__client_request_id=row.client_request_id).order_by("-created_at", "-pk")
    run = runs.first()
    if run is None:
        return None
    if (run.github_repo or run.workflow not in {"article_generation", "direct_generate", "confirmed_topic"} or
            run.run_request.get("cms_goal_operation_id") != str(operation.pk) or
            run.run_request.get("cms_goal_id") != str(operation.pk) or
            run.run_request.get("publishing_channel_artifact") != original["publishing_channel_artifact"] or
            operation.payload["source_run_id"] is not None and operation.payload["source_run_id"] not in {row.client_request_id, run.run_id}):
        raise WebsiteAuthorityError("website_operation_changed", "The original native first-article run changed.")
    return run


def advance(operation, *, lease_id, now):
    """Start at most once; ambiguous starts recover through the original outbox."""
    value = authority.current(operation, lease_id=lease_id)
    existing = original_dispatch(operation)
    if existing is not None:
        run = original_run(operation, existing)
        if run is not None:
            return run
        # The original dispatch scheduler owns delivery and unknown outcomes.
        # Reserving a local mirror is safe and never repeats the canonical
        # start that generated the analytics ID and paid request digest.
        from .dispatch_binding import reserve_portable_dispatch_intent
        return reserve_portable_dispatch_intent(organization=value.context.organization,
            workflow="article_generation", actor_id=existing.payload["actor_id"], payload=deepcopy(existing.payload["request"]))
    if budget.exhaustion(operation, now=now, action="first-article-generation") is not None:
        raise WebsiteAuthorityError("website_prepare_blocked", "The native first-article budget is exhausted.")
    data = operation.payload["generation_data"]
    if not isinstance(data, dict):
        raise WebsiteAuthorityError("website_operation_changed", "The original native first-article topic is missing.")
    from . import channel_artifacts, vibe_marketing_views as views
    used = operation.receipt["native_budget"]["budget_used"]
    scope = channel_artifacts.prepare_generation(value.context, value.config, user=value.user)
    response = views._start_article_generation_for_channel(user=value.user, context=value.context,
        data={**deepcopy(data), "delivery_mode": "content_only", "delivery_mode_explicit": True},
        channel_artifact_scope=scope, cms_goal_scope=authority.CmsGoalDispatch(str(operation.pk), lease_id),
        website_contract={"goal_budget": {"version": 1, "goal_id": value.goal["goal_id"], "scope_digest": digest(value.scope),
            "remaining_active_seconds": settings.ARTICLES_GOAL_ACTIVE_SECONDS - (used.get("active_seconds") or 0),
            "remaining_model_usd": settings.ARTICLES_GOAL_MODEL_USD - (used.get("model_cost") or 0)}})
    if response.status_code >= 400:
        # If the canonical transaction committed before the response failed,
        # the next lease must discover that same outbox rather than start anew.
        original = original_dispatch(operation)
        if original is not None:
            return original_run(operation, original)
        error = WebsiteAuthorityError("website_prepare_step_failed",
            "The original native article start is waiting for its current prerequisite.", retryable=response.status_code >= 500)
        error.result = response.data
        raise error
    original = original_dispatch(operation)
    if original is None:
        raise WebsiteAuthorityError("website_prepare_dependency_transient", "The native article dispatch is still being recorded.", retryable=True)
    return original_run(operation, original)
