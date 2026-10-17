"""Authenticated native setup reserves one durable site goal and original hold."""

from copy import deepcopy
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_budget as budget
from . import cms_goal_consent as consent
from . import cms_goal_contract as goals
from .cms_capture import _consent as current_connection
from .cms_goal_evidence import current_scope
from .cms_owner_connection import _request_id
from .website_contract import WebsiteAuthorityError


def original_payload(context, *, binding, policy, user, choice):
    """Construct private native execution references after actual policy authority."""
    return {"schema_version": 1, "financial_subject_version": 2, "binding": deepcopy(binding),
        "credential_operation_id": policy["policy"]["credential_operation_id"], "policy_operation_id": policy["operation_id"],
        "policy_digest": policy["policy"]["digest"], "company_id": str(context.company.pk), "requested_by_user_id": str(user.pk),
        "consent": deepcopy(choice), "capture_operation_id": None, "generation_data": None, "source_run_id": None, "failures": 0}


def start(context, config, *, user, data, now=None):
    """Accept the current native policy/price once; replay never charges again."""
    if not authority.enabled(context.organization.pk):
        raise WebsiteAuthorityError("publishing_adapter_required", "Native article setup is not available.", status=404)
    request_id, now = _request_id(data), now or timezone.now()
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=context.organization.pk)
        for item in (context.organization, context.profile, context.company, user, config): item.refresh_from_db()
        binding = current_connection(context, config, user, data)
        WebsiteConnection.objects.select_for_update().get(pk=binding["connection_id"])
        expected, policy = current_scope(context, config, user=user)
        if (data.get("expected_policy_id") != policy["operation_id"] or data.get("review_policy") != policy["policy"]["review_policy"] or
                data.get("billing_confirmed") is not True or type(data.get("expected_cost_points")) is not int):
            raise WebsiteAuthorityError("website_prepare_blocked", "Review the current native publication choice and first-article price.", status=422)
        if config.website_connection.operations.filter(action="cms-setup", generation=binding["generation"],
                payload__company_id=binding["company_id"]).exists():
            raise WebsiteAuthorityError("website_prepare_dependency_transient", "The original native article setup is already recorded.", retryable=True)
        key = f"cms-goal:{binding['connection_id']}:{binding['generation']}:{request_id}"
        prior = WebsiteConnectionOperation.objects.filter(idempotency_key=key).first()
        latest = config.website_connection.operations.filter(action=authority.ACTION, generation=binding["generation"]).order_by("-created_at", "-pk").first()
        selected = prior or latest
        if selected is not None:
            value = authority.current(selected, user=user, states={selected.state})
            if (value.scope != expected or value.consent["answers"]["billing"]["expected_points"] != data["expected_cost_points"] or
                    prior is not None and latest is not None and prior.pk != latest.pk):
                raise WebsiteAuthorityError("operation_key_conflict", "The original native setup request has different saved choices.")
            # A terminal goal is still the same first article. Recovery must
            # explicitly reuse its original source/payment or settle it before
            # reserving a successor; a new click is not a new debit authority.
            return selected
        identifier = str(uuid4())
        goal = goals.new_goal(identifier, expected, now=now)
        try:
            choice = consent.seal(goal, user_id=user.pk, review_policy=policy["policy"]["review_policy"],
                expected_points=data["expected_cost_points"], confirmed=data["billing_confirmed"], now=now)
        except goals.CmsGoalError:
            raise WebsiteAuthorityError("website_prepare_blocked", "Review the original native first-article price.", status=422) from None
        operation, _created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "id": identifier, "connection": config.website_connection, "generation": binding["generation"], "action": authority.ACTION,
            "state": "pending", "next_attempt_at": now,
            "payload": original_payload(context, binding=binding, policy=policy, user=user, choice=choice),
            "receipt": {"goal": goal, "native_budget": budget.initial(goal), "status": "starting", "current_step": "capture_existing"}})
        billing.reserve(operation, user=user, now=now)
        return operation


def projection(operation, *, user=None):
    """Expose progress without provider origins, keys, private consent or finances."""
    value = authority.current(operation, user=user, states={operation.state})
    result = {"version": 1, "id": str(operation.pk), "state": operation.state, "status": operation.receipt.get("status", "starting"),
        "currentStep": operation.receipt.get("current_step", "capture_existing"), "revision": value.goal["revision"],
        "milestones": [{"name": row["name"], "state": row["state"], "required": row["required"]} for row in value.goal["milestones"]],
        "nextRetryAt": operation.next_attempt_at.isoformat() if operation.next_attempt_at else None,
        "sourceRunId": operation.payload["source_run_id"]}
    choice = operation.receipt.get("user_action")
    if isinstance(choice, dict) and choice.get("id") in {"review_draft", "connect_cms"}:
        result["userAction"] = {key: deepcopy(choice[key]) for key in ("id", "runId", "stageId", "reviewRevision") if key in choice}
    if isinstance(operation.receipt.get("public_url"), str): result["publicUrl"] = operation.receipt["public_url"]
    if getattr(settings, "ARTICLES_CMS_STATUS_ENABLED", False):
        from .cms_goal_status import project
        from .website_doctor import safe_identifier
        current = project(operation, value.context, value.config, user=value.user, binding=value.scope["binding"], now=timezone.now())
        if current is None:
            raise WebsiteAuthorityError("website_connection_changed", "The current native articles scope changed.")
        result.update(milestones=current["milestones"], nextRetryAt=current["nextAttemptAt"],
            state={"working": operation.state, "needs_you": "needs_user", "done": "completed", "stopped": "needs_attention"}[current["state"]],
            status={"working": "working", "needs_you": "owner_action_required", "done": "first_article_live", "stopped": "verification_required"}[current["state"]])
        result.pop("publicUrl", None)
        result.pop("userAction", None)
        result["sourceRunId"] = safe_identifier(result["sourceRunId"])
        if current.get("publicUrl") is not None: result["publicUrl"] = current["publicUrl"]
        if current["state"] == "needs_you":
            result["userAction"] = {key: current["action"][key] for key in ("id", "runId", "stageId", "reviewRevision") if key in current["action"]}
    return result


def current_projection(context, config, *, user):
    """Read only this selected owner's currently bound native goal."""
    if (not authority.enabled(context.organization.pk) or config.website_connection is None or
            "_cms_binding" not in (config.website_connection.capabilities or {})):
        return None
    current_scope(context, config, user=user)
    operation = config.website_connection.operations.filter(action=authority.ACTION,
        generation=config.website_connection.generation).order_by("-created_at", "-pk").first()
    return projection(operation, user=user) if operation is not None else None
